import os
import glob
import re

ROUTERS_DIR = os.path.join("backend", "app", "routers")

files_to_process = glob.glob(os.path.join(ROUTERS_DIR, "*.py"))

def process_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # We want to replace `async def` with `def` EXCEPT for `refresh.py` because it has SSE and streaming.
    if "refresh.py" in filepath:
        return

    # Replace `async def` with `def`
    content = re.sub(r'async def ', 'def ', content)
    
    # Replace `await ` with empty string
    content = re.sub(r'await\s+', '', content)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Processed {filepath}")

for f in files_to_process:
    process_file(f)
