#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo ">>> Running backend tests..."

# The backend dir has bundled pydantic/pydantic_core with Linux .so files
# that can't load on macOS. Temporarily hide them so pytest uses the
# system-installed versions.
HIDDEN_DIRS=()
for pkg in pydantic pydantic_core; do
    if [ -d "$BACKEND_DIR/$pkg" ]; then
        mv "$BACKEND_DIR/$pkg" "$BACKEND_DIR/_hidden_${pkg}"
        HIDDEN_DIRS+=("$pkg")
    fi
done

# Also hide dist-info dirs
for di in "$BACKEND_DIR"/pydantic-*.dist-info "$BACKEND_DIR"/pydantic_core-*.dist-info; do
    if [ -d "$di" ]; then
        mv "$di" "${di}.hidden"
    fi
done

cleanup() {
    # Restore hidden dirs
    for pkg in "${HIDDEN_DIRS[@]}"; do
        if [ -d "$BACKEND_DIR/_hidden_${pkg}" ]; then
            mv "$BACKEND_DIR/_hidden_${pkg}" "$BACKEND_DIR/$pkg"
        fi
    done
    for di in "$BACKEND_DIR"/*.dist-info.hidden; do
        if [ -d "$di" ]; then
            mv "$di" "${di%.hidden}"
        fi
    done
}
trap cleanup EXIT

cd "$BACKEND_DIR"
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -40

echo ">>> Backend tests complete."
