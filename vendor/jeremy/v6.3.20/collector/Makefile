SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= ./asic-pool/.env
COMPOSE_FILE ?= ./docker-compose.yml
SNAPSHOT_DIR ?= ./data-restore

POOL_IMAGE ?=
BLOCKDAG_NODE_IMAGE ?=
BLOCKDAG_NODE1_IMAGE ?=

STACK_SERVICES := pool-db bdag-miner-node-1 asic-pool

.PHONY: help check-mining-address check-snapshot-url prepare-dirs login-docker pull-images download-snapshot restore-node-snapshot up-stack down-stack ps-stack logs-stack collector sync-coordinator-once codex-handoff

help:
	@echo "Targets:"
	@echo "  make login-docker                 # Login to Docker Hub"
	@echo "  make pull-images                  # Pull pool and node images"
	@echo "  make download-snapshot SNAPSHOT_URL=https://...   # Download .tar.gz snapshot"
	@echo "  make restore-node-snapshot [SNAPSHOT_ARCHIVE=...] # Restore snapshot into the node"
	@echo "  make up-stack [MINING_ADDRESS=0x...] # Start node + pool stack"
	@echo "  make down-stack                   # Stop and remove stack"
	@echo "  make ps-stack                     # Show running stack services"
	@echo "  make logs-stack                   # Tail logs for stack services"
	@echo "  make collector                    # Run local collector API on 127.0.0.1:9280"
	@echo "  make sync-coordinator-once        # Show large catch-up coordinator decision"
	@echo "  make codex-handoff                # Write ops/runtime/codex-handoff.md"

check-mining-address:
	@ADDR="$(MINING_ADDRESS)"; \
	if [[ -z "$$ADDR" ]] && [[ -f "$(ENV_FILE)" ]]; then \
		ADDR="$$(grep -E '^MINING_ADDRESS=' "$(ENV_FILE)" | tail -n1 | cut -d= -f2-)"; \
	fi; \
	if [[ -z "$$ADDR" ]]; then \
		echo "ERROR: MINING_ADDRESS is required."; \
			echo "Set it in $(ENV_FILE) or pass: make up-stack MINING_ADDRESS=0x1234..."; \
		exit 1; \
	fi; \
	if [[ ! "$$ADDR" =~ ^0x[0-9a-fA-F]{40}$$ ]]; then \
		echo "ERROR: MINING_ADDRESS must look like a 42-char EVM address (0x + 40 hex chars)."; \
		exit 1; \
	fi

check-snapshot-url:
	@if [[ -z "$(SNAPSHOT_URL)" ]]; then \
		echo "ERROR: SNAPSHOT_URL is required."; \
		echo "Example: make download-snapshot SNAPSHOT_URL=https://.../backup.tar.gz"; \
		exit 1; \
	fi

prepare-dirs:
	@mkdir -p "$(SNAPSHOT_DIR)" ./data/node1 ./data/postgres

login-docker:
	@echo "Docker Hub keeps one credential set per registry host on a client."
	@echo "Use one account/token with access to BOTH repos, then run: docker login"
	docker login

pull-images:
	@set -a; \
		if [[ -f "$(ENV_FILE)" ]]; then . "$(ENV_FILE)"; fi; \
		set +a; \
		NODE_BASE="$${BLOCKDAG_NODE_IMAGE:-nathanbdagengineering/bdag-gameday-binaries:mainnet}"; \
		NODE1_IMG="$(BLOCKDAG_NODE1_IMAGE)"; \
		POOL_IMG="$(POOL_IMAGE)"; \
		if [[ -z "$$NODE1_IMG" ]]; then NODE1_IMG="$${BLOCKDAG_NODE1_IMAGE:-$$NODE_BASE}"; fi; \
		if [[ -z "$$POOL_IMG" ]]; then POOL_IMG="$${POOL_IMAGE:-nathanbdagnetwork/asic-pool:latest}"; fi; \
		echo "Pulling node1 image: $$NODE1_IMG"; docker pull "$$NODE1_IMG"; \
		echo "Pulling pool image: $$POOL_IMG"; docker pull "$$POOL_IMG"

download-snapshot: check-snapshot-url prepare-dirs
	@SNAPSHOT_BASENAME="$${SNAPSHOT_NAME:-$${SNAPSHOT_URL##*/}}"; \
	SNAPSHOT_BASENAME="$${SNAPSHOT_BASENAME%%\?*}"; \
	if [[ -z "$$SNAPSHOT_BASENAME" ]]; then SNAPSHOT_BASENAME="miner-snapshot.tar.gz"; fi; \
	SNAPSHOT_ARCHIVE="$(SNAPSHOT_DIR)/$$SNAPSHOT_BASENAME"; \
	echo "Downloading snapshot to $$SNAPSHOT_ARCHIVE"; \
	curl -fL --retry 3 --retry-delay 2 -o "$$SNAPSHOT_ARCHIVE" "$(SNAPSHOT_URL)"; \
	echo "Snapshot saved: $$SNAPSHOT_ARCHIVE"

restore-node-snapshot: prepare-dirs
	@SNAPSHOT_ARCHIVE_LOCAL="$(SNAPSHOT_ARCHIVE)"; \
	if [[ -z "$$SNAPSHOT_ARCHIVE_LOCAL" ]]; then SNAPSHOT_ARCHIVE_LOCAL="$$(ls -1t "$(SNAPSHOT_DIR)"/*.tar.gz 2>/dev/null | head -n1)"; fi; \
	if [[ -z "$$SNAPSHOT_ARCHIVE_LOCAL" ]]; then \
		echo "ERROR: SNAPSHOT_ARCHIVE not set and no .tar.gz found in $(SNAPSHOT_DIR)."; \
		echo "Run download first or pass SNAPSHOT_ARCHIVE=./data-restore/your-backup.tar.gz"; \
		exit 1; \
	fi; \
	if [[ ! -f "$$SNAPSHOT_ARCHIVE_LOCAL" ]]; then \
		echo "ERROR: Snapshot archive not found: $$SNAPSHOT_ARCHIVE_LOCAL"; \
		exit 1; \
	fi; \
	echo "Restoring node1 from $$SNAPSHOT_ARCHIVE_LOCAL"; \
	tar --delay-directory-restore --no-overwrite-dir --overwrite --no-same-owner --no-same-permissions --mode=u+rwX -xzf "$$SNAPSHOT_ARCHIVE_LOCAL" -C ./data/node1; \
	echo "Node1 restore complete."

up-stack:
	@if [[ -n "$(MINING_ADDRESS)" ]]; then \
		if [[ -f "$(ENV_FILE)" ]]; then \
			MINING_ADDRESS="$(MINING_ADDRESS)" docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" up -d $(STACK_SERVICES); \
		else \
			MINING_ADDRESS="$(MINING_ADDRESS)" docker compose -f "$(COMPOSE_FILE)" up -d $(STACK_SERVICES); \
		fi; \
	else \
		if [[ -f "$(ENV_FILE)" ]]; then \
			docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" up -d $(STACK_SERVICES); \
		else \
			docker compose -f "$(COMPOSE_FILE)" up -d $(STACK_SERVICES); \
		fi; \
	fi

down-stack:
	@if [[ -f "$(ENV_FILE)" ]]; then \
		docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" down; \
	else \
		docker compose -f "$(COMPOSE_FILE)" down; \
	fi

ps-stack:
	@if [[ -f "$(ENV_FILE)" ]]; then \
		docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" ps $(STACK_SERVICES); \
	else \
		docker compose -f "$(COMPOSE_FILE)" ps $(STACK_SERVICES); \
	fi

logs-stack:
	@if [[ -f "$(ENV_FILE)" ]]; then \
		docker compose --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)" logs -f --tail=150 $(STACK_SERVICES); \
	else \
		docker compose -f "$(COMPOSE_FILE)" logs -f --tail=150 $(STACK_SERVICES); \
	fi

collector:
	python3 ops/collector.py

sync-coordinator-once:
	python3 ops/sync_coordinator.py --once

codex-handoff:
	@PYTHONPATH=ops python3 -c 'from pool_ops import make_handoff; print(make_handoff())'
