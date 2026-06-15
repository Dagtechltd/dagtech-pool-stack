# Nodeworker Liveness And Stack Naming RCA - 2026-06-03

## Incident

On 2026-06-03 the mining stack containers were running, but the node container
was not serving a live BlockDAG child process. The pool and dashboard could see
container liveness, but miners did not receive usable current jobs because the
nodeworker wrapper remained up after the `blockdag-node` child was killed.

The root-cause log sequence was:

- nodeworker liveness probe exceeded the 1 minute timeout while the node was
  doing heavy chain/state catch-up work.
- nodeworker stopped and killed the inner binary.
- the container stayed running with only nodeworker alive.
- watchdog defaults were not aligned to the current Compose service names, and
  the child guard did not detect the packaged `blockdag-node` child.

## Fix

The stack now treats `node`, `pool`, and `postgres` as the current service
names.

The node child guard now:

- detects the packaged `blockdag-node` child executable;
- defaults to guarding `node`;
- resolves the actual Compose service label before restart;
- falls back to direct `docker restart`/`docker start` when Compose targeting
  fails;
- omits missing env files when building Compose commands.

The node entrypoint now adds `--health.liveness-timeout=5m` by default unless an
operator explicitly supplies another nodeworker liveness timeout. This avoids
turning normal constrained-host catch-up pressure into a stale running
container with no child node.

## Build Gates

CI now includes runtime naming checks that verify:

- Compose/dashboard defaults use `postgres,node,pool`;
- release installers and the ARM64 builder emit current names and `node` RPC
  URLs;
- watchdog and peer-refresh defaults know the current topology;
- the hardening validator fails if current naming checks drift.

## Live Deployment Note

Runtime guards report and act on `node`, `pool`, and `postgres`; if Docker
Compose adds project or ordinal suffixes to concrete container names, the guards
resolve the service label before restart.
