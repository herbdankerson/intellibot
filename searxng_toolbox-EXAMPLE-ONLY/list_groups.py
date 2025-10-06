import json

CONFIG_FILE = "searxng_toolbox/engine_groups.json"

def list_groups():
    """
    Lists all saved search engine groups from the config file.
    """
    try:
        with open(CONFIG_FILE, 'r') as f:
            try:
                groups = json.load(f)
            except json.JSONDecodeError:
                groups = {}
    except FileNotFoundError:
        groups = {}

    if not groups:
        print("No search engine groups have been saved yet.")
        return

    print("Available Search Engine Groups:")
    for group_name, engines in groups.items():
        print(f"- {group_name}: {', '.join(engines)}")

if __name__ == "__main__":
    list_groups()
