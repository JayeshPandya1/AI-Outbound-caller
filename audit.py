import asyncio
import os
import ssl
import certifi
import aiohttp
from dotenv import load_dotenv
from livekit import api as lk_api

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

async def main():
    # 1. Check Supabase
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    
    if not supabase_url or not supabase_key:
        print("CRITICAL: Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env.")
        return

    # Use direct Supabase import
    from supabase import create_client
    supabase_client = create_client(supabase_url, supabase_key)
    
    # Query database setting
    res = supabase_client.table("settings").select("value").eq("key", "OUTBOUND_TRUNK_ID").maybe_single().execute()
    supabase_id = res.data.get("value") if res.data else None
    
    print("\n" + "="*60)
    print(f"Supabase ID: [{supabase_id}]")
    print("="*60 + "\n")

    # 2. Check LiveKit
    raw_url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")

    if not all([raw_url, key, secret]):
        print("CRITICAL: Missing LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET in .env.")
        return

    http_url = raw_url.replace("wss://", "https://").replace("ws://", "http://")
    
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        try:
            lk = lk_api.LiveKitAPI(url=http_url, api_key=key, api_secret=secret, session=session)
            
            print("Querying LiveKit Server...")
            response = await lk.sip.list_sip_outbound_trunk(lk_api.ListSIPOutboundTrunkRequest())
            
            # Access 'items' property from Twirp response
            trunks = list(response.items) if hasattr(response, "items") else []
            
            print(f"Server Outbound Trunks Found: {len(trunks)}")
            print("-" * 60)
            
            if len(trunks) == 0:
                print("🚨 SERVER IS EMPTY: The setup script failed or pointed to the wrong URL.")
                print(f"Raw response from server: {response}")
            else:
                matched = False
                for trunk in trunks:
                    print(f" - Server Trunk ID: [{trunk.sip_trunk_id}] (Name: {trunk.name}, Domain: {trunk.address})")
                    if trunk.sip_trunk_id == supabase_id:
                        matched = True
                
                if matched:
                    print("\n✅ SUCCESS: Supabase ID matches one of the registered Server Trunk IDs.")
                else:
                    server_ids = [t.sip_trunk_id for t in trunks]
                    print(f"\n🚨 MISMATCH: Supabase has [{supabase_id}] but Server has {server_ids}.")
            
            print("-" * 60 + "\n")
            await lk.aclose()
        except Exception as exc:
            print(f"Error during LiveKit query: {exc}")

if __name__ == "__main__":
    asyncio.run(main())
