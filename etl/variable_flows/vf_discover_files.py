"""
Variable flow for discovering input files.

This module provides a simple Prefect task that traverses a directory and
returns a list of file paths matching a provided glob pattern.  It is
designed to be used by high-level ingestion flows as the first step of the
pipeline.
"""

from pathlib import Path
from typing import List

from prefect import task


@task
def discover_files(source_path: str, file_glob: str) -> List[str]:
    """Return a list of files under ``source_path`` matching ``file_glob``.

    Args:
        source_path: Path to the directory containing input files.
        file_glob: A glob pattern (e.g. ``*.csv``) relative to ``source_path``.

    Returns:
        A list of absolute file paths as strings.
    """
    root = Path(source_path)
    if not root.exists():
        return []
    return [
        str(p)
        for p in root.rglob(file_glob)
        if p.is_file()
    ]
