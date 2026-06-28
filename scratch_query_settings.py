import asyncio
import os
import sys
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(".env")

async def main():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Missing Supabase credentials in .env")
        return

    client = create_client(url, key)
    res = client.table("settings").select("key, value").execute()
    
    output = []
    output.append("\nSUPABASE SETTINGS:")
    output.append("-" * 80)
    for row in res.data or []:
        k = row.get("key")
        v = row.get("value")
        if not k:
            continue
        # Hide sensitive credentials
        if any(sec in k for sec in ["KEY", "SECRET", "PASS", "TOKEN"]):
            output.append(f"{k:<30} : [REDACTED] (len: {len(v) if v else 0})")
        else:
            output.append(f"{k:<30} : {v}")
    output.append("-" * 80)
    
    text = "\n".join(output) + "\n"
    sys.stdout.buffer.write(text.encode('utf-8', errors='replace'))

if __name__ == "__main__":
    asyncio.run(main())
