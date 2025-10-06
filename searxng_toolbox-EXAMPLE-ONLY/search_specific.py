import requests
import json
import argparse

SEARXNG_URL = "http://127.0.0.1:8888"
OUTPUT_FILE = "searxng_toolbox/search_results.json"

def specific_search(query, engines):
    """
    Performs a search using a specific list of engines and saves the results.
    """
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
            
        print(f"Successfully performed search for '{query}' on engines: {engines}.")
        print(f"Results saved to {OUTPUT_FILE}")

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to SearxNG instance at {SEARXNG_URL}: {e}")
    except json.JSONDecodeError:
        print("Error: Could not decode the JSON response from SearxNG.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform a search on specific engines using the local SearxNG instance.")
    parser.add_argument("query", help="The search query.")
    parser.add_argument("engines", help="A comma-separated list of engines to use (e.g., 'google,bing').")
    args = parser.parse_args()
    
    specific_search(args.query, args.engines)
