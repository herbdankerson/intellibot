from typing import List, Dict, Optional, Any, Union
import asyncio
import logging
from mcp.server.fastmcp import FastMCP
from pypi_api import PyPIAPI

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize FastMCP server
mcp = FastMCP("pypi")

# Initialize PyPIAPI client
client = PyPIAPI()


@mcp.tool()
async def get_package_info(package_name: str, version: Optional[str] = None) -> Dict:
    logging.info(f"Getting package info for: {package_name}, version: {version if version else 'latest'}")
    """
    Get detailed information about a package

    Args:
        package_name: The name of the package
        version: Optional, specify the version number

    Returns:
        Dictionary containing package information
    """
    try:
        result = await client.get_package_info(package_name, version)
        return result
    except Exception as e:
        logging.error(f"An error occurred while getting package info: {str(e)}")
        return {"error": f"An error occurred while getting package info: {str(e)}"}


@mcp.tool()
async def search_packages(query: str) -> List[Dict]:
    logging.info(f"Searching packages with query: {query}")
    """
    Search for packages

    Args:
        query: Search keyword

    Returns:
        List of dictionaries containing search results
    """
    try:
        results = await client.search_packages(query)
        return results
    except Exception as e:
        logging.error(f"An error occurred while searching packages: {str(e)}")
        return [{"error": f"An error occurred while searching packages: {str(e)}"}]


@mcp.tool()
async def get_package_releases(package_name: str) -> Dict:
    logging.info(f"Getting package releases for: {package_name}")
    """
    Get all release versions of a package

    Args:
        package_name: The name of the package

    Returns:
        Dictionary containing all release versions
    """
    try:
        result = await client.get_package_releases(package_name)
        return result
    except Exception as e:
        logging.error(f"An error occurred while getting package releases: {str(e)}")
        return {"error": f"An error occurred while getting package releases: {str(e)}"}


@mcp.tool()
async def get_package_stats(package_name: str) -> Dict:
    logging.info(f"Getting package stats for: {package_name}")
    """
    Get download statistics for a package

    Args:
        package_name: The name of the package

    Returns:
        Dictionary containing download statistics
    """
    try:
        result = await client.get_package_stats(package_name)
        return result
    except Exception as e:
        logging.error(f"An error occurred while getting package stats: {str(e)}")
        return {"error": f"An error occurred while getting package stats: {str(e)}"}


if __name__ == "__main__":
    logging.info("Starting PyPI MCP server")
    # Initialize and run the server
    mcp.run(transport='stdio')