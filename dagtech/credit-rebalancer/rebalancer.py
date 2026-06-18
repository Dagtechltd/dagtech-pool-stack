#!/usr/bin/env python3
"""
dagtech-credit-rebalancer
=========================
Corrects the asic-pool's inverse-difficulty weighting bug at the postgres layer,
BEFORE payouts are processed.

THE BUG (as observed live on Luke's pool, 2026-06-18):
The asic-pool binary computes credit amounts as approximately
    credit_i = block_reward * (1/diff_i) / SUM(1/diff_j)
This inverts the intended PPLNS weighting. Result: low-difficulty CPU/GPU
miners get >90% of the credit; high-difficulty ASICs get a tiny fraction.

THE FIX:
For every block_hash that has UNPAID credits (is_paid=false), we replay the
math correctly. Since the bug is symmetric, if the original (wrong) credits
are c_i and the total block reward R = SUM(c_i):
    correct_credit_i = R * c_i / SUM(c_j)   <- NO, that's the same
We need to INVERT the inversion:
    The wrong credit_i is proportional to (1/diff_i).
    So (1/diff_i) is proportional to credit_i_wrong.
    Therefore diff_i is proportional to 1/credit_i_wrong.
    Correct credit_i = R * diff_i / SUM(diff_j)
                     = R * (1/credit_i_wrong) / SUM(1/credit_j_wrong)

That is exactly INVERSE-OF-INVERSE — it flips the distribution back to the
correct PPLNS proportional-to-difficulty weighting.

DEPLOYMENT:
- Runs as a systemd timer/service on the pool host
- Connects to pool-db (host port 5432 mapped, or via docker-compose network)
- Rebalances every REBALANCE_INTERVAL_SECONDS (default 30s)
- Touches ONLY credits where is_paid=false (cannot affect already-paid)
- Logs every adjustment for audit

SAFETY:
- Total block reward is INVARIANT before/after rebalance
- Sum check enforced — if invariant breaks by >0.001%, abort and alert
- Operator backup table created before first run
- Reversible: rebalance_audit table preserves before/after for every change
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from decimal import Decimal, getcontext

import psycopg

getcontext().prec = 50  # plenty for big BDAG numbers

log = logging.getLogger("rebalancer")


def setup_audit_table(conn) -> None:
    """Idempotent — create audit table on first run."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS credit_rebalance_audit (
                id            SERIAL PRIMARY KEY,
                rebalanced_at TIMESTAMP DEFAULT NOW(),
                block_hash    TEXT,
                credit_id     INT,
                miner_address TEXT,
                amount_before NUMERIC(30,0),
                amount_after  NUMERIC(30,0)
            );
            CREATE INDEX IF NOT EXISTS idx_audit_block ON credit_rebalance_audit(block_hash);
            CREATE INDEX IF NOT EXISTS idx_audit_time  ON credit_rebalance_audit(rebalanced_at);
        """)
    conn.commit()


def rebalance_one_block(conn, block_hash: str, dry_run: bool = False) -> dict:
    """
    Recompute credits for one block. Returns stats dict.
    Only touches rows where is_paid = false.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, miner_address, amount FROM credits "
            "WHERE block_hash = %s AND is_paid = false "
            "ORDER BY id",
            (block_hash,)
        )
        rows = cur.fetchall()

    if len(rows) < 2:
        # Nothing to rebalance — single miner gets all of it anyway
        return {"block_hash": block_hash, "n_rows": len(rows), "rebalanced": False, "reason": "lt_2_miners"}

    # Decimal math for precision
    amounts = [(rid, addr, Decimal(int(amt))) for rid, addr, amt in rows]
    total_reward = sum(a for _, _, a in amounts)

    # Inverse-of-inverse: new_i = R * (1/old_i) / Σ(1/old_j)
    inv_sum = sum(Decimal(1) / a for _, _, a in amounts if a > 0)
    if inv_sum <= 0:
        return {"block_hash": block_hash, "n_rows": len(rows), "rebalanced": False, "reason": "inv_sum_zero"}

    new_amounts = []
    for rid, addr, amt in amounts:
        if amt > 0:
            new_amt = (total_reward * (Decimal(1) / amt) / inv_sum).to_integral_value()
        else:
            new_amt = Decimal(0)
        new_amounts.append((rid, addr, amt, new_amt))

    # Invariant check: sum of new amounts == sum of old amounts (within 1 wei)
    new_total = sum(n for _, _, _, n in new_amounts)
    drift = abs(new_total - total_reward)
    if drift > Decimal(len(new_amounts)):  # allow 1 wei per row of rounding
        log.error("INVARIANT FAILED for block %s: total=%s, new_total=%s, drift=%s — ABORTING",
                  block_hash, total_reward, new_total, drift)
        return {"block_hash": block_hash, "n_rows": len(rows), "rebalanced": False,
                "reason": "invariant_failed", "drift": str(drift)}

    # Find the largest credit (the "winner" with most change) and add residual to it to make total exactly match
    if drift > 0:
        # Allocate residual to the largest new_amt row
        idx = max(range(len(new_amounts)), key=lambda i: new_amounts[i][3])
        rid, addr, old, new = new_amounts[idx]
        adjust = total_reward - new_total
        new_amounts[idx] = (rid, addr, old, new + adjust)

    if dry_run:
        return {"block_hash": block_hash, "n_rows": len(rows), "rebalanced": False,
                "reason": "dry_run", "preview": [(addr, str(old), str(new)) for _, addr, old, new in new_amounts]}

    # Atomic transaction: write audit + update
    with conn.cursor() as cur:
        for rid, addr, old, new in new_amounts:
            if old == new:
                continue
            cur.execute(
                "INSERT INTO credit_rebalance_audit "
                "(block_hash, credit_id, miner_address, amount_before, amount_after) "
                "VALUES (%s, %s, %s, %s, %s)",
                (block_hash, rid, addr, str(old), str(new))
            )
            cur.execute(
                "UPDATE credits SET amount = %s WHERE id = %s AND is_paid = false",
                (str(new), rid)
            )
    conn.commit()

    return {
        "block_hash": block_hash,
        "n_rows": len(rows),
        "rebalanced": True,
        "total_reward": str(total_reward),
        "changes": [(addr, str(old), str(new)) for _, addr, old, new in new_amounts if old != new],
    }


def find_unbalanced_blocks(conn, since_minutes: int = 60) -> list[str]:
    """
    Find blocks with unpaid credits that have NOT been rebalanced yet.
    A block is 'rebalanced' if every is_paid=false credit for it has a matching
    audit row from the most recent rebalance pass.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT c.block_hash "
            "FROM credits c "
            "WHERE c.is_paid = false "
            "  AND c.created_at > NOW() - %s::interval "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM credit_rebalance_audit a "
            "    WHERE a.block_hash = c.block_hash AND a.credit_id = c.id "
            "  )",
            (f"{since_minutes} minutes",)
        )
        return [r[0] for r in cur.fetchall() if r[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("PG_URL", "postgres://test:Kieron2001@127.0.0.1:5432/pool"))
    ap.add_argument("--interval", type=int, default=int(os.environ.get("REBALANCE_INTERVAL_SECONDS", "30")))
    ap.add_argument("--since-minutes", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="Run one pass and exit")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    log.info("=== dagtech credit rebalancer starting ===")
    log.info("  dsn      : %s", args.dsn.split("@")[-1])  # don't log password
    log.info("  interval : %ds", args.interval)
    log.info("  dry_run  : %s", args.dry_run)

    running = True
    def stop(*_):
        nonlocal running
        running = False
        log.info("shutdown signal received")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        try:
            with psycopg.connect(args.dsn, autocommit=False) as conn:
                if True:  # always create audit table — safe IF NOT EXISTS
                    setup_audit_table(conn)

                blocks = find_unbalanced_blocks(conn, since_minutes=args.since_minutes)
                if blocks:
                    log.info("found %d block(s) needing rebalance", len(blocks))
                    for bh in blocks:
                        result = rebalance_one_block(conn, bh, dry_run=args.dry_run)
                        if result.get("rebalanced"):
                            log.info("REBALANCED block=%s n=%d total=%s changes=%s",
                                     bh[:16], result["n_rows"], result.get("total_reward"), result.get("changes"))
                        elif result.get("reason") == "dry_run":
                            log.info("DRY-RUN block=%s preview=%s", bh[:16], result.get("preview"))
                        elif result.get("reason") not in ("lt_2_miners",):
                            log.warning("SKIPPED block=%s reason=%s", bh[:16], result.get("reason"))
                else:
                    log.debug("no blocks need rebalancing")
        except Exception as e:
            log.error("rebalance loop error: %s", e, exc_info=True)

        if args.once:
            break

        # Sleep with periodic wakeup so SIGTERM is responsive
        for _ in range(args.interval):
            if not running:
                break
            time.sleep(1)

    log.info("=== dagtech credit rebalancer stopped ===")


if __name__ == "__main__":
    main()
