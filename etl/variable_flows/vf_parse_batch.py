"""
Variable flow for parsing input files into row dictionaries.

This module defines a simple Prefect task that consumes a list of file
paths and produces a list of dictionaries where each dictionary represents
a row of data.  It currently supports CSV files and relies on Python's
standard ``csv`` module.  You can extend this task to support additional
formats such as JSON or Parquet.
"""

import csv
from typing import Dict, List

from prefect import task


@task
def parse_files(files: List[str]) -> List[Dict[str, str]]:
    """Parse a collection of CSV files into a list of row dictionaries.

    Each file is read using ``csv.DictReader`` which infers headers from
    the first row.  All rows across all input files are concatenated into
    a single list.  If no files are provided an empty list is returned.

    Args:
        files: A list of file paths to parse.

    Returns:
        A list of dictionaries, one per row.
    """
    rows: List[Dict[str, str]] = []
    for file in files:
        try:
            with open(file, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(dict(row))
        except FileNotFoundError:
            # If a file disappears between discovery and parse we skip it.
            continue
    return rows
