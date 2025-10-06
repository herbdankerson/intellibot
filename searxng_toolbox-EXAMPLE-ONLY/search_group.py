import requests
import json
import argparse

SEARXNG_URL = "http://127.0.0.1:8888"
OUTPUT_FILE = "searxng_toolbox/search_results.json"
CONFIG_FILE = "searxng_toolbox/engine_groups.json"

def search_group(query, group_name):
    """
    Performs a search using a saved group of engines.
    """
    try:
        with open(CONFIG_FILE, 'r') as f:
            try:
                groups = json.load(f)
            except json.JSONDecodeError:
                print(f"Error: Could not decode the JSON in {CONFIG_FILE}.")
                return
    except FileNotFoundError:
        print(f"Error: Configuration file {CONFIG_FILE} not found.")
        return

    if group_name not in groups:
        print(f"Error: Group '{group_name}' not found in {CONFIG_FILE}.")
        print("Available groups are:")
        for name in groups.keys():
            print(f"- {name}")
        return

    engines = ','.join(groups[group_name])
    
    params = {
        'q': query,
        'format': 'json',
        'engines': engines
    }
    try:
        response = requests.post(f"{SEARXNG_URL}/search", params=params)
        response.raise_for_status()
        results = response.json()
        
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(results, f, indent=2)
            
        print(f"Successfully performed search for '{query}' on group '{group_name}'.")
        print(f"Results saved to {OUTPUT_FILE}")

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to SearxNG instance at {SEARXNG_URL}: {e}")
    except json.JSONDecodeError:
        print("Error: Could not decode the JSON response from SearxNG.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform a search on a saved group of engines.")
    parser.add_argument("query", help="The search query.")
    parser.add_argument("group_name", help="The name of the engine group to use.")
    args = parser.parse_args()
    
    search_group(args.query, args.group_name)
