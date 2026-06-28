import asyncio
import os
import sys
from datetime import datetime, timezone
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
    
    # Query all errors from today
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Querying logs from {today_iso} onwards...")
    res = client.table("error_logs").select("*").gte("timestamp", today_iso).order("timestamp", desc=True).execute()
    
    output = []
    output.append(f"\nSUPABASE ERRORS TODAY ({len(res.data or [])} entries):")
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
