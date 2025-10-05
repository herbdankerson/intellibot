"""
Transform tasks used to normalise and clean data rows.

A transform task accepts a list of row dictionaries and returns a new list of
row dictionaries after applying some transformation.  Use transforms to
standardise column names, cast datatypes, trim whitespace, etc.  They can be
composed and referenced by name in the registry to create per-table pipelines.
"""

from typing import Dict, List


def standardize_colnames(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Transform column names to lowercase with underscores.

    This simple example transform normalises the keys of each row dictionary by
    converting them to lowercase and replacing spaces with underscores.  It
    returns a new list of dictionaries and does not mutate the input.

    Args:
        rows: A list of row dictionaries.

    Returns:
        A list of row dictionaries with normalised keys.
    """
    transformed: List[Dict[str, str]] = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            new_key = key.strip().lower().replace(" ", "_")
            new_row[new_key] = value
        transformed.append(new_row)
    return transformed
