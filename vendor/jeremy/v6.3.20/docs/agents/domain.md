# Domain Docs

This repo is configured as multi-context.

The shared domain and operating context for this repo lives in the sibling `../codex-memory` repository. This stack repo and the upstream component repos share that memory source.

## Before exploring, read these

1. If `../codex-memory/AGENTS.md` exists, read it first.
2. Follow its "Required First Read" section.
3. For BlockDAG node/pool/dashboard/compose stack work, also read the BlockDAG-specific durable references named there.
4. If repo-local context docs exist, read `CONTEXT-MAP.md` at the repo root and the relevant per-context `CONTEXT.md` files it points to.
5. Read `docs/adr/` for system-wide decisions and context-scoped ADRs such as `src/<context>/docs/adr/` when they exist.

If any of these files don't exist, proceed silently. The producer skill (`/grill-with-docs`) creates repo-local context and ADR files lazily when terms or decisions get resolved.

## Use the glossary's vocabulary

When output names a domain concept, use the term as defined in the relevant shared memory file or repo-local `CONTEXT.md`.

## Flag ADR conflicts

If output contradicts an existing memory decision or ADR, surface it explicitly rather than silently overriding.
