import json
import argparse

CONFIG_FILE = "searxng_toolbox/engine_groups.json"

def save_group(group_name, engines):
    """
    Saves or updates a named group of search engines in the config file.
    """
    try:
        with open(CONFIG_FILE, 'r') as f:
            try:
                groups = json.load(f)
            except json.JSONDecodeError:
                groups = {}
    except FileNotFoundError:
        groups = {}

    engine_list = [engine.strip() for engine in engines.split(',')]
    groups[group_name] = engine_list
    
    with open(CONFIG_FILE, 'w') as f:
        json.dump(groups, f, indent=2)
        
    print(f"Successfully saved group '{group_name}' with engines: {', '.join(engine_list)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Save a named group of search engines.")
    parser.add_argument("group_name", help="The name for the engine group (e.g., 'tech_news').")
    parser.add_argument("engines", help="A comma-separated list of engines to include in the group (e.g., 'google-news,bing-news').")
    args = parser.parse_args()
    
    save_group(args.group_name, args.engines)
