import requests
import json

SEARXNG_URL = "http://127.0.0.1:8888"

def list_available_engines():
    """
    Fetches the server configuration and lists all available search engines.
    """
    try:
        response = requests.get(f"{SEARXNG_URL}/config")
        response.raise_for_status()
        config = response.json()
        
        print("Available Search Engines:")
        engine_list = config.get('engines', [])
        engine_names = sorted([engine.get('name') for engine in engine_list if engine.get('name')])
        for name in engine_names:
            print(f"- {name}")
            
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to SearxNG instance at {SEARXNG_URL}: {e}")
    except json.JSONDecodeError:
        print("Error: Could not decode the JSON response from SearxNG.")

if __name__ == "__main__":
    list_available_engines()
