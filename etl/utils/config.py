from pathlib import Path
import yaml


def load_registry(path: str) -> dict:
    """Load the ETL table registry from a YAML file.

    The registry defines default settings and per-table configuration that drive
    how data is ingested and loaded into your database. See etl/registry/etl_tables.yml
    for an example schema.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A dictionary containing the parsed configuration.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Registry file not found: {p}")
    with p.open("r") as f:
        return yaml.safe_load(f)
