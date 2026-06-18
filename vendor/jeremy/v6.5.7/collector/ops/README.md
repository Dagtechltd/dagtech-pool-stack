# Collector Ops

This folder contains read-only collectors and passive monitoring helpers for the
BlockDAG ASIC pool stack.

Run the collector API locally:

```bash
python3 ops/collector.py
```

Default URL:

```text
http://127.0.0.1:9280
```

The collector API is intentionally passive. It serves JSON status, earnings,
global chain, incident, P2P, and normalized node/pool log payloads for the Go
dashboard. It does not restart services, clean-restore chain data, configure
miners, save ASIC credentials, or provide action tokens.
