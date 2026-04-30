#!/bin/bash
# Sentinel watchdog script for ResearchClaw pipeline runs
# Usage: sentinel.sh <run_dir>

set -euo pipefail

RUN_DIR="${1:-}"

if [ -z "$RUN_DIR" ]; then
    echo "Usage: sentinel.sh <run_dir>" >&2
    exit 1
fi

if [ ! -d "$RUN_DIR" ]; then
    echo "Error: run directory does not exist: $RUN_DIR" >&2
    exit 1
fi

HEARTBEAT_FILE="$RUN_DIR/heartbeat.json"

# Watch the heartbeat file and report status
if [ -f "$HEARTBEAT_FILE" ]; then
    LAST_STAGE=$(python3 -c "import json; d=json.load(open('$HEARTBEAT_FILE')); print(d.get('last_stage_name','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
    echo "Run directory: $RUN_DIR"
    echo "Last stage: $LAST_STAGE"
else
    echo "Run directory: $RUN_DIR"
    echo "No heartbeat file found yet."
fi
