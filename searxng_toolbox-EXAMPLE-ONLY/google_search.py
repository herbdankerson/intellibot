import requests
import json
import argparse

SEARXNG_URL = "http://127.0.0.1:8888"
OUTPUT_FILE = "searxng_toolbox/search_results.json"

def google_search(query):
    """
    Performs a search using only the Google engine and saves the results.
    """
    params = {
        'q': query,
        'format': 'json',
        'engines': 'google'
    }
    try:
        response = requests.post(f"{SEARXNG_URL}/search", params=params)
        response.raise_for_status()
        results = response.json()
        
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(results, f, indent=2)
            
        print(f"Successfully performed Google search for '{query}'.")
        print(f"Results saved to {OUTPUT_FILE}")

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to SearxNG instance at {SEARXNG_URL}: {e}")
    except json.JSONDecodeError:
        print("Error: Could not decode the JSON response from SearxNG.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform a Google search using the local SearxNG instance.")
    parser.add_argument("query", help="The search query.")
    args = parser.parse_args()
    
    google_search(args.query)
