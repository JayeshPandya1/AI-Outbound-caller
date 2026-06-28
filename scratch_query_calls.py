import asyncio
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(".env")

async def main():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Missing Supabase credentials in .env")
        return

    print(f"Connecting to Supabase at: {url}")
    client = create_client(url, key)
    
    # Query last 15 call logs
    res = client.table("call_logs").select("*").order("timestamp", desc=True).limit(15).execute()
    
    print("\nLAST 15 CALL LOGS:")
    print("-" * 120)
    print(f"{'Timestamp':<25} | {'Phone Number':<15} | {'Lead Name':<15} | {'Outcome':<10} | {'Duration (s)':<12} | {'Reason'}")
    print("-" * 120)
    for row in res.data or []:
        ts = row.get("timestamp", "")
        phone = row.get("phone_number", "")
        name = row.get("lead_name", "") or ""
        outcome = row.get("outcome", "")
        duration = row.get("duration_seconds", 0)
        reason = row.get("reason", "")
        print(f"{ts:<25} | {phone:<15} | {name:<15} | {outcome:<10} | {duration:<12} | {reason}")
    print("-" * 120)

if __name__ == "__main__":
    asyncio.run(main())
