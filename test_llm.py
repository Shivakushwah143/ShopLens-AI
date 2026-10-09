import asyncio
import logging
from app.core.config import get_settings
from app.core.llm import llm_client

logging.basicConfig(level=logging.DEBUG)

async def test():
    settings = get_settings()
    print("Testing model:", settings.llm_model_final)
    try:
        res = await llm_client.text_completion(
            model=settings.llm_model_final,
            system_prompt="Say hello",
            user_prompt="Hello"
        )
        print("RESULT:", res)
    except Exception as e:
        print("ERROR:", e)

if __name__ == "__main__":
    asyncio.run(test())
