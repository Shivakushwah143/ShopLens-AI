import asyncio
import httpx
from app.core.config import get_settings

async def test():
    settings = get_settings()
    url = f"{settings.gemini_api_base}/models?key={settings.llm_api_key}"
    async with httpx.AsyncClient() as client:
        res = await client.get(url)
        data = res.json()
        for m in data.get('models', []):
            print(m['name'])

if __name__ == "__main__":
    asyncio.run(test())
