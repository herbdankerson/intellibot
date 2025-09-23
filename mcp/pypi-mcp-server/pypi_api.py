import httpx
from typing import Dict, List, Optional, Union
import json

class PyPIAPI:
    def __init__(self):
        self.base_url = "https://pypi.org/pypi"
        
    async def get_package_info(self, package_name: str, version: Optional[str] = None) -> Dict:
        """获取包的详细信息"""
        url = f"{self.base_url}/{package_name}"
        if version:
            url += f"/{version}"
        url += "/json"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()["info"]
    
    async def search_packages(self, query: str) -> List[Dict]:
        """搜索包"""
        from bs4 import BeautifulSoup
        
        url = "https://pypi.org/search/"
        params = {
            "q": query,
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            
            for package in soup.select('.package-snippet'):
                name = package.select_one('.package-snippet__name').text.strip()
                version = package.select_one('.package-snippet__version').text.strip()
                description = package.select_one('.package-snippet__description').text.strip()
                
                results.append({
                    'name': name,
                    'version': version,
                    'description': description
                })
            
            return results
    
    async def get_package_releases(self, package_name: str) -> Dict:
        """获取包的所有发布版本"""
        url = f"{self.base_url}/{package_name}/json"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            return data.get("releases", {})
    
    async def get_package_stats(self, package_name: str) -> Dict:
        """获取包的下载统计信息"""
        url = f"https://pypistats.org/api/packages/{package_name}/recent"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

# 使用示例
async def main():
    pypi = PyPIAPI()
    
    # 获取包信息
    package_info = await pypi.get_package_info("requests")
    print("Package Info:", package_info.keys())
    
    # 搜索包
    search_results = await pypi.search_packages("web framework")
    print("Search Results:", json.dumps(search_results, indent=2))
    
    # 获取发布版本
    releases = await pypi.get_package_releases("django")
    print("Releases:", json.dumps(releases, indent=2))
    
    # 获取下载统计
    stats = await pypi.get_package_stats("flask")
    print("Stats:", json.dumps(stats, indent=2))

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())