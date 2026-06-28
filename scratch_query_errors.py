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
    res = client.table("error_logs").select("*").order("timestamp", desc=True).limit(100).execute()
    
    output = []
    output.append("\nSUPABASE ERROR LOGS:")
    output.append("-" * 120)
    for row in res.data or []:
        timestamp = row.get("timestamp")
        source = row.get("source")
        message = row.get("message")
        detail = row.get("detail", "")
        level = row.get("level", "")
        output.append(f"Time: {timestamp} | Source: {source} | Level: {level}")
        output.append(f"Message: {message}")
        if detail:
            output.append(f"Detail: {detail}")
        output.append("-" * 120)
        
    text = "\n".join(output) + "\n"
    sys.stdout.buffer.write(text.encode('utf-8', errors='replace'))

if __name__ == "__main__":
    asyncio.run(main())
