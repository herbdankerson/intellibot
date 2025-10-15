#!/bin/bash
set -euo pipefail
python /app/render_config.py
exec litellm --config /tmp/config.rendered.yaml --port 4000
