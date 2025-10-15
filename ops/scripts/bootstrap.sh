#!/bin/sh

# Bootstraps runtime containers by installing the Freshbot package and applying
# the ParadeDB schema before launching the requested process.

set -eu

if [ -d "/workspace/freshbot" ]; then
    echo "[bootstrap] Installing freshbot editable package..."
    pip install --no-cache-dir --editable /workspace/freshbot >/tmp/freshbot-install.log
fi

if [ -f "/app/ops/scripts/migrate.py" ]; then
    echo "[bootstrap] Applying database schema..."
    attempt=0
    until python /app/ops/scripts/migrate.py >/tmp/schema-migrate.log 2>&1; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 5 ]; then
            echo "[bootstrap] Failed to apply schema after $attempt attempts."
            cat /tmp/schema-migrate.log
            exit 1
        fi
        echo "[bootstrap] Schema apply failed (attempt $attempt); retrying in 5s..."
        sleep 5
    done
fi

if python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('freshbot') else 1)" >/tmp/freshbot-check.log 2>&1; then
    echo "[bootstrap] Applying Freshbot registry..."
    python -m freshbot.devtools.registry_loader --apply --registry-dir /workspace/freshbot/src/freshbot/registry >/tmp/registry-apply.log 2>&1 || {
        echo "[bootstrap] Registry apply failed:"
        cat /tmp/registry-apply.log
        exit 1
    }
fi

exec "$@"
