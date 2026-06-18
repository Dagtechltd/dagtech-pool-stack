#!/bin/bash
set -euo pipefail

# =============================================================================
# Snapshot Export Script for Blockdag Pool-Stack
# =============================================================================
# This script:
# 1. Stops the node container cleanly (if it was running) so DB/freezer files are consistent
# 2. Copies the datadir from the stopped container (docker cp works on stopped containers)
# 3. Creates a snapshot export using blockdag-node (PATH, repo bin/, ../blockdag-corechain/build/bin/bdag,
#    or copy from container — prefer host paths to avoid docker cp when /var/lib/docker is full).
#
# Usage: ./scripts/export-snapshot.sh [container_name] [output_file]
#
# Env: SNAPSHOT_EXPORT_TMPDIR — base dir for large temp copies (default: $TMPDIR or /tmp).
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Large working dirs for docker cp + snap export (needs ~2× chain DB peak during export).
# Override if /tmp is small or full: export SNAPSHOT_EXPORT_TMPDIR=/path/with/free_space
SNAPSHOT_TMP_BASE="${SNAPSHOT_EXPORT_TMPDIR:-${TMPDIR:-/tmp}}"

# Default values
CONTAINER_NAME="${1:-pool-stack-docker-node-1}"
OUTPUT_FILE="${2:-$SCRIPT_DIR/../release-downloads/latest.bdsnap}"

# Second-stage export dir (cleaned on success in create_snapshot; trap removes on failure)
SNAPSHOT_EXPORT_TEMP=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# All logging must go to stderr so stdout stays clean for $(command) captures
# (e.g. TEMP_DIR=$(copy_datadir) must only receive the path on stdout).
log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} WARNING: $1" >&2
}

log_error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} ERROR: $1" >&2
}

# Check if container exists (does not start it — export copies from a stopped container for consistency).
check_container() {
    log "Checking if container '$CONTAINER_NAME' exists..."
    
    if ! docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_error "Container '$CONTAINER_NAME' not found!"
        log "Available containers:"
        docker ps -a --format '{{.Names}}' | head -10
        exit 1
    fi
    
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log "Container is running (will be stopped before copy for a consistent datadir)."
    else
        log "Container is already stopped (copying frozen filesystem state)."
    fi
}

# Stop the node container cleanly
stop_node() {
    log "Stopping node container '$CONTAINER_NAME'..."
    
    # Check if running
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log "Container is running, stopping gracefully..."
        
        # Try graceful stop first (sends SIGTERM)
        docker stop "$CONTAINER_NAME" --timeout 30
        
        # Verify it stopped
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            log_warn "Container did not stop gracefully, forcing stop..."
            docker kill "$CONTAINER_NAME"
        fi
    else
        log "Container is already stopped."
    fi
}

# Copy datadir from container
copy_datadir() {
    local temp_dir="$SNAPSHOT_TMP_BASE/bdag-export-$(date +%s)"
    
    log "Creating temporary directory: $temp_dir"
    mkdir -p "$temp_dir"
    
    # No docker exec here: container may be stopped; use docker cp only.
    
    # Copy the mainnet data directory (always available)
    log "Copying datadir from container..."
    log "Container: $CONTAINER_NAME"
    log "Source: /var/lib/bdagStack/node/mainnet"
    log "Destination: $temp_dir/mainnet-data"
    
    if docker cp "$CONTAINER_NAME:/var/lib/bdagStack/node/mainnet" "$temp_dir/mainnet-data"; then
        if [ -d "$temp_dir/mainnet-data" ]; then
            log "Mainnet data copied successfully ($(ls -la "$temp_dir/mainnet-data" | wc -l) items)"
        else
            log_error "docker cp succeeded but directory not found!"
            return 1
        fi
    else
        log_error "Failed to copy mainnet data!"
        log_error "Exit code: $?"
        return 1
    fi
    
    # Optional: extra BdagChain copy at alternate paths (mainnet export already includes .../mainnet/BdagChain).
    log "Trying optional standalone BdagChain copy..."
    if docker cp "$CONTAINER_NAME:/var/lib/bdagStack/node/BdagChain" "$temp_dir/BdagChain" 2>/dev/null; then
        log "BdagChain copied (node/BdagChain)"
    elif docker cp "$CONTAINER_NAME:/var/lib/bdagStack/node/mainnet/BdagChain" "$temp_dir/BdagChain" 2>/dev/null; then
        log "BdagChain copied (duplicate of path inside mainnet — usually redundant)"
    else
        log "Using BdagChain inside mainnet-data only (normal layout)"
    fi
    
    if docker cp "$CONTAINER_NAME:/tmp/snapshot-candidate.bdsnap" "$temp_dir/existing-snapshot.bdsnap" 2>/dev/null; then
        log "Copied existing /tmp/snapshot-candidate.bdsnap from container (if any)"
    fi
    
    echo "$temp_dir"
}

# Verify the copied data
verify_data() {
    local data_dir="$1"
    
    log "Verifying exported data..."
    
    # Check mainnet directory
    if [ ! -d "$data_dir/mainnet-data" ]; then
        log_error "mainnet-data directory not found!"
        return 1
    fi
    
    # Check BdagChain directory
    if [ ! -d "$data_dir/BdagChain" ]; then
        log_warn "BdagChain directory not found, using mainnet data instead"
    fi
    
    # List contents for verification
    log "Exported structure:"
    ls -la "$data_dir/"
    
    return 0
}

# Prefer host binary so we avoid docker cp (requires free space under Docker's graph driver).
resolve_bdag_binary() {
    local extract_to="$1"
    local sibling_bdag=""
    sibling_bdag="$(dirname "$PROJECT_ROOT")/blockdag-corechain/build/bin/bdag"

    if command -v blockdag-node &>/dev/null; then
        command -v blockdag-node
        return 0
    fi
    if [ -x "$PROJECT_ROOT/bin/blockdag-node" ]; then
        echo "$PROJECT_ROOT/bin/blockdag-node"
        return 0
    fi
    if [ -x "$sibling_bdag" ]; then
        log "Using blockdag sibling build (avoids docker cp): $sibling_bdag"
        echo "$sibling_bdag"
        return 0
    fi

    log "No blockdag-node on host; copying from container '$CONTAINER_NAME' → $extract_to..."
    mkdir -p "$(dirname "$extract_to")"
    if docker cp "$CONTAINER_NAME:/usr/local/bin/blockdag-node" "$extract_to"; then
        chmod +x "$extract_to"
        echo "$extract_to"
        return 0
    fi

    log_error "docker cp failed for $CONTAINER_NAME:/usr/local/bin/blockdag-node → $extract_to"
    return 1
}

# Create the snapshot export
create_snapshot() {
    local data_dir="$1"
    local output_path="$(dirname "$OUTPUT_FILE")"
    
    log "Creating snapshot export..."
    
    # Ensure output directory exists
    mkdir -p "$output_path"
    
    local bdag_binary=""
    # Cache container copy under repo bin/ so the next run hits resolve_bdag_binary without docker cp.
    local bdag_cache="$PROJECT_ROOT/bin/blockdag-node"
    if ! bdag_binary="$(resolve_bdag_binary "$bdag_cache")"; then
        log_error "blockdag-node binary not found!"
        log "Options: build/copy to pool-stack-docker/bin/blockdag-node, install on PATH, keep ../blockdag-corechain/build/bin/bdag, or free Docker disk (docker system df; docker builder prune -af) so copying from the container works."
        return 1
    fi
    log "Using blockdag-node: $bdag_binary"
    
    # Create snapshot using blockdag-node snap export command
    local temp_export="$SNAPSHOT_TMP_BASE/snapshot-export-$(date +%s)"
    SNAPSHOT_EXPORT_TEMP="$temp_export"
    export SNAPSHOT_EXPORT_TEMP
    
    log "Running snapshot export (work dir: $temp_export)..."
    
    # First, clean up any existing export files
    rm -rf "$temp_export" 2>/dev/null || true
    mkdir -p "$temp_export"

    # snap export --datadir must match the node layout: the directory that *contains* BdagChain
    # (same as the container's /var/lib/bdagStack/node/mainnet), not a lone BdagChain tree.
    local export_datadir=""
    if [ -d "$data_dir/mainnet-data" ]; then
        cp -r "$data_dir/mainnet-data" "$temp_export/"
        export_datadir="$temp_export/mainnet-data"
        log "Using full mainnet datadir for snap export ($export_datadir)"
    elif [ -d "$data_dir/BdagChain" ]; then
        cp -r "$data_dir/BdagChain" "$temp_export/"
        export_datadir="$temp_export"
        log_warn "Only a standalone BdagChain copy is available; prefer exporting from full mainnet datadir when possible"
    else
        log_error "No valid data directory found!"
        return 1
    fi

    if [ ! -d "$export_datadir/BdagChain" ]; then
        log_error "BdagChain missing under export datadir (expected $export_datadir/BdagChain)"
        return 1
    fi

    log "snap export --datadir=$export_datadir"
    "$bdag_binary" snap export \
        --datadir="$export_datadir" \
        --path="$OUTPUT_FILE" \
        2>&1 | tee "$temp_export/export.log"
    
    # Verify the export was created
    if [ ! -f "$OUTPUT_FILE" ]; then
        log_error "Snapshot export failed! Output file not found: $OUTPUT_FILE"
        return 1
    fi
    
    local file_size=$(stat -c%s "$OUTPUT_FILE")
    log "Snapshot created successfully: $OUTPUT_FILE (${file_size} bytes)"

    log "Removing export working directory..."
    rm -rf "$temp_export"
    SNAPSHOT_EXPORT_TEMP=""
    
    # Show some stats
    log "Snapshot contents:"
    if command -v tar &>/dev/null; then
        tar -tzf "$OUTPUT_FILE" 2>/dev/null | head -20 || echo "(not a tarball or empty)"
    fi
    
    return 0
}

# Cleanup function
cleanup() {
    local exit_code=$?
    
    # Clean up temp directories (datadir copy + snap export staging — latter can be huge if export fails)
    if { [ -n "${TEMP_DIR:-}" ] && [ -d "$TEMP_DIR" ]; } ||
        { [ -n "${SNAPSHOT_EXPORT_TEMP:-}" ] && [ -d "$SNAPSHOT_EXPORT_TEMP" ]; }; then
        log "Cleaning up temporary files..."
    fi
    if [ -n "${TEMP_DIR:-}" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
    if [ -n "${SNAPSHOT_EXPORT_TEMP:-}" ] && [ -d "$SNAPSHOT_EXPORT_TEMP" ]; then
        rm -rf "$SNAPSHOT_EXPORT_TEMP"
    fi
    
    # If the node was running before we stopped it for export, bring it back (success or failure).
    if [ "${WAS_RUNNING_BEFORE_EXPORT:-false}" = "true" ]; then
        log "Starting container '$CONTAINER_NAME' (was running before export)..."
        docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    
    exit $exit_code
}

# Trap for cleanup on error/signal
trap cleanup EXIT INT TERM

# =============================================================================
# Main Execution
# =============================================================================
main() {
    log "=============================================="
    log "Blockdag Snapshot Export Script"
    log "=============================================="
    log "Container: $CONTAINER_NAME"
    log "Output:    $OUTPUT_FILE"
    log "=============================================="
    
    # Step 1: Check container exists
    check_container
    
    # Step 2: Remember if node was up; stop before copy so LevelDB / freezer files are consistent
    WAS_RUNNING_BEFORE_EXPORT=false
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        WAS_RUNNING_BEFORE_EXPORT=true
    fi
    export WAS_RUNNING_BEFORE_EXPORT
    
    if [ "$WAS_RUNNING_BEFORE_EXPORT" = "true" ]; then
        stop_node
    fi
    
    # Step 3: Copy datadir from stopped container
    TEMP_DIR=$(copy_datadir)
    
    # Step 4: Verify data
    if ! verify_data "$TEMP_DIR"; then
        log_error "Data verification failed!"
        exit 1
    fi
    
    # Step 5: Create snapshot export
    if ! create_snapshot "$TEMP_DIR"; then
        log_error "Snapshot creation failed!"
        exit 1
    fi
    
    log "=============================================="
    log "Snapshot export completed successfully!"
    log "=============================================="
    
    # Show the exported file info
    log "Exported file details:"
    ls -lh "$OUTPUT_FILE"
    
    return 0
}

# Run main function
main "$@"
