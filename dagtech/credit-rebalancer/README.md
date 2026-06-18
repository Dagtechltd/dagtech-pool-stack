# DagTech Credit Rebalancer

> **Critical fix for `asic-pool` v1.3.23 (and earlier)**
> Corrects an inverse-difficulty share-weighting bug that causes high-difficulty
> ASIC miners to be massively under-credited and low-difficulty CPU/GPU miners
> to be massively over-credited. Apply this on every pool host you operate
> until a patched pool binary is released.

## The bug

The compiled `mining-pool` Go binary computes credits as

    credit_i  ∝  1 / share_difficulty_i

The correct PPLNS formula is

    credit_i  ∝  share_difficulty_i

Result on a live mixed pool (observed 2026-06-18 on Luke's box):

| Miner | Vardiff | Wrong split | Correct split |
|---|---|---|---|
| Low-diff GPU/CPU | 0.5–10 | **93%** | 0.5% |
| Mid-diff ASIC | ~50 | 6% | 8% |
| High-diff ASIC (X30 etc.) | thousands | 0.5% | **91%** |

## How the rebalancer fixes it

A small sidecar container scans `credits` for `is_paid=false` rows every 30 seconds.
For each block, it applies the **inverse-of-inverse** correction:

    correct_amount_i = R × (1/wrong_amount_i) / Σ(1/wrong_amount_j)
    where R = SUM(wrong_amount_j) per block

This cancels the bug exactly. Total block reward is invariant — payouts still add
up to the same number, just distributed correctly. Every change is logged to
`credit_rebalance_audit`.

## Install

```bash
git clone https://github.com/Dagtechltd/dagtech-pool-stack
cd dagtech-pool-stack/dagtech/credit-rebalancer
sudo ./install.sh
```

The installer:
1. Builds the `dagtech/credit-rebalancer:0.1.0` docker image
2. Writes `/etc/dagtech-credit-rebalancer.env` with auto-detected pool network + DB URL
3. Installs + enables `dagtech-credit-rebalancer.service` systemd unit

## Verify it's running

```bash
systemctl status dagtech-credit-rebalancer.service
journalctl -u dagtech-credit-rebalancer.service -f
```

You should see lines like:

    REBALANCED block=addadd684c71eb16 n=3 total=... changes=[(0x..., before, after), ...]

## Safety

- **Reversible**: `systemctl disable --now dagtech-credit-rebalancer.service`
- **Backup**: the installer does NOT touch your existing data. Run a backup first if
  you want belt-and-braces:
  ```sql
  CREATE TABLE credits_backup AS SELECT * FROM credits;
  ```
- **Audit trail**: every adjustment recorded in `credit_rebalance_audit`
  (block_hash, credit_id, miner_address, amount_before, amount_after, rebalanced_at)
- **Invariant**: total block reward preserved bit-exact (1 wei rounding goes to the
  largest credit per block)

## Removal / rollback

```bash
sudo systemctl disable --now dagtech-credit-rebalancer.service
sudo rm /etc/systemd/system/dagtech-credit-rebalancer.service
sudo rm /etc/dagtech-credit-rebalancer.env
# Optional: restore from your backup table
# UPDATE credits c SET amount = b.amount FROM credits_backup b WHERE c.id = b.id AND c.is_paid = false;
```

## When the upstream binary fix lands

Once a patched `mining-pool` binary is released, you can stop and remove the
rebalancer — it does nothing if the binary already computes credits correctly
(the rebalance becomes a no-op against the correct ratios).

## Source

- Single Python file: `rebalancer.py` (~250 LOC, no external deps beyond psycopg)
- Dockerfile: minimal `python:3.12-slim` + `psycopg[binary]`
- License: MIT (this rebalancer); see `LICENSE-NOTICE.md` in the pool-stack root for
  full attribution including upstream Jeremy components.

— DagTech Ltd, 2026-06-18
