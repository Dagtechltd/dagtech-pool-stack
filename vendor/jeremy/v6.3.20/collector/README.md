# BlockDAG Collector

Read-only JSON collector for the BlockDAG ASIC pool stack.

The collector gathers node, pool, earnings, global chain, incident, P2P, and
log data for the Go dashboard. It does not expose repair actions, miner
configuration actions, action tokens, or an HTML UI.

## Included

- `ops/collector.py`: read-only HTTP API.
- `ops/pool_ops.py`: stack, node, miner, wallet, and status collection library.
- `ops/p2p_guard.py`, `ops/sync_coordinator.py`, samplers, and reporting helpers
  that collect or summarize state.
- `ops/tests/`: focused regression tests carried from the live stack.

ASIC tuning tools have been moved out of this repo to the stack repo's
`ASIC-tools/` folder and are not installed or run by default.

## API

```text
GET /api/status
GET /api/earnings
GET /api/global
GET /api/global/pool-earnings
GET /api/logs/node?tail=240
GET /api/logs/pool?tail=240
GET /api/sampler
GET /api/incidents
GET /api/p2p
```

Every `POST` request returns `405 Method Not Allowed`.

## Run Locally

```bash
BDAG_COLLECTOR_BIND=127.0.0.1 BDAG_COLLECTOR_PORT=9280 python3 ops/collector.py
```

Runtime state, chain data, reports, logs, tokens, saved ASIC admin passwords,
local `.env` files, and wallet or private-key material are intentionally not in
this repository.

## Validation

```bash
python3 -m py_compile ops/*.py
python3 -m pytest ops/tests
```
