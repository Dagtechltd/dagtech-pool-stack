# GitOps Branching Regimen

This repository is part of the BlockDAG mining pool stack. The same branch naming regimen applies across:

- `dashboard`
- `pool-stack-docker`
- `blockdag-corechain`
- `pool`

## Permanent Branches

- `main`: production-stable history only. Release tags are cut from `main` after release validation.
- `develop`: integration branch for completed feature work. Feature branches merge here before release candidate branches are created.

## Working Branch Names

Use lowercase kebab-case after the slash. Include the repo/component prefix when the branch name may be viewed outside its repo.

- `feature/<component>-<short-purpose>`: normal feature work.
- `fix/<component>-<short-purpose>`: non-emergency bug fixes targeting `develop`.
- `hotfix/<component>-<short-purpose>`: urgent production fixes branched from `main`.
- `release/<stack-or-component>-YYYYMMDD-rcN`: release candidate stabilization branches.
- `experiment/<component>-<short-purpose>`: throwaway or time-boxed experiments.
- `chore/<component>-<short-purpose>`: maintenance, dependency, CI, docs, or cleanup work.

Examples:

- `feature/corechain-peer-discovery`
- `feature/dashboard-miner-health-tab`
- `fix/pool-credit-idempotency`
- `release/pool-stack-20260524-rc4`
- `hotfix/pool-submit-regression`

## Merge Flow

1. Branch new work from `develop`.
2. Keep feature branches short-lived and focused on one change.
3. Open pull requests back to `develop` when feature work is complete.
4. Do not merge feature branches directly to `main`.
5. Create release branches from `develop` after the involved repos have compatible source manifests.
6. Merge release branches back to `develop` and `main` after validation.
7. Branch hotfixes from `main`, then merge the hotfix back to both `main` and `develop`.

## Cross-Repo Release Manifests

Every stack release candidate must record the exact branch and commit for all participating repos:

- `dashboard`
- `pool-stack-docker`
- `blockdag-corechain`
- `pool`

The manifest belongs in the release notes or release package provenance. A stack release is not reproducible unless all four source refs are recorded.

## Protection Expectations

- Protect `main` and `develop`.
- Require pull requests for `main` and `develop`.
- Require status checks before merging.
- Require linear or merge-commit history consistently per repo; do not mix ad hoc styles inside the same repo.
- Delete merged feature branches after the merge is complete.
