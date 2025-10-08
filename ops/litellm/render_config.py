from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_PATH = Path("/app/config.yaml")
OUTPUT_PATH = Path("/tmp/config.rendered.yaml")


def expand_env(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: expand_env(val) for key, val in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def main() -> None:
    data = yaml.safe_load(CONFIG_PATH.read_text())
    expanded = expand_env(data)
    OUTPUT_PATH.write_text(yaml.safe_dump(expanded, sort_keys=False))


if __name__ == "__main__":
    main()
