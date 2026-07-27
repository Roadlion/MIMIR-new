import asyncio
import os
import sys

# add backend dir to sys path so imports work
sys.path.append(os.path.abspath("backend"))

from app.services.voice_service import generate_mimir_speech

async def main():
    try:
        url = await generate_mimir_speech("Hello, this is a test to reproduce the bug.")
        print(f'SUCCESS URL: {url}')
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
