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
    # Retrieve credentials
    raw_url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    
    if not all([raw_url, key, secret]):
        print("CRITICAL: Missing LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET in .env file.")
        return

    # Bulletproof URL scheme conversion
    http_url = raw_url.replace("wss://", "https://").replace("ws://", "http://")
    
    print("\n" + "="*50)
    print("--- LIVEKIT AUDIT CONFIGURATION ---")
    print(f"Original URL from .env: {raw_url}")
    print(f"Connecting to HTTP URL: {http_url}")
    print(f"API Key:                {key}")
    print(f"API Secret:             {'[LOADED]' if secret else '[MISSING]'}")
    print("="*50 + "\n")

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        try:
            lk = lk_api.LiveKitAPI(url=http_url, api_key=key, api_secret=secret, session=session)
            
            print("Fetching Outbound SIP Trunks...")
            response = await lk.sip.list_sip_outbound_trunk(lk_api.ListSIPOutboundTrunkRequest())
            print(f"Raw Outbound Response: {response}")
            
            trunks = list(response.results) if hasattr(response, "results") else []
            print(f"\nFound {len(trunks)} Outbound Trunk(s):")
            print("-" * 60)
            for trunk in trunks:
                print(f"SIP Trunk ID: {trunk.sip_trunk_id}")
                print(f"Name:         {trunk.name}")
                print(f"Address:      {trunk.address}")
                print(f"Numbers:      {list(trunk.numbers)}")
                print("-" * 60)
                
            print("\nFetching Inbound SIP Trunks...")
            inbound_response = await lk.sip.list_sip_inbound_trunk(lk_api.ListSIPInboundTrunkRequest())
            print(f"Raw Inbound Response: {inbound_response}")
            
            await lk.aclose()
        except Exception as exc:
            print(f"Error during audit of trunks: {exc}")

if __name__ == "__main__":
    asyncio.run(main())
