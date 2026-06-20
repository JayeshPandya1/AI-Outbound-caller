import asyncio
import os
import ssl
import certifi
import aiohttp
from dotenv import load_dotenv
from livekit import api as lk_api

# Load environment variables from .env
load_dotenv(".env")

async def main():
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    
    sip_domain = os.getenv("VOBIZ_SIP_DOMAIN")
    username = os.getenv("VOBIZ_USERNAME")
    password = os.getenv("VOBIZ_PASSWORD")
    outbound_number = os.getenv("VOBIZ_OUTBOUND_NUMBER")

    if not all([url, key, secret, sip_domain, username, password, outbound_number]):
        print("Error: Missing required LiveKit or Vobiz environment variables in .env file.")
        print(f"LIVEKIT_URL: {url}")
        print(f"LIVEKIT_API_KEY: {key}")
        print(f"LIVEKIT_API_SECRET: {'***' if secret else None}")
        print(f"VOBIZ_SIP_DOMAIN: {sip_domain}")
        print(f"VOBIZ_USERNAME: {username}")
        print(f"VOBIZ_PASSWORD: {'***' if password else None}")
        print(f"VOBIZ_OUTBOUND_NUMBER: {outbound_number}")
        return

    print("Authenticating with self-hosted LiveKit server...")
    
    # Set up SSL context to handle self-hosted certificates safely
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        try:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            
            # Construct SIPOutboundTrunkInfo
            trunk_info = lk_api.SIPOutboundTrunkInfo(
                name="Vobiz Outbound",
                address=sip_domain,
                transport=lk_api.SIP_TRANSPORT_UDP,
                auth_username=username,
                auth_password=password
            )
            trunk_info.numbers.append(outbound_number)
            
            # Construct CreateSIPOutboundTrunkRequest
            request = lk_api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)
            
            print("Registering SIP Outbound Trunk...")
            created_trunk = await lk.sip.create_sip_outbound_trunk(request)
            
            print("\n" + "="*50)
            print("SUCCESS: SIP Outbound Trunk registered!")
            print(f"SIP Trunk ID: {created_trunk.sip_trunk_id}")
            print(f"SIP Trunk Name: {created_trunk.name}")
            print(f"Address: {created_trunk.address}")
            print(f"Allowed Numbers: {list(created_trunk.numbers)}")
            print("="*50 + "\n")
            
            await lk.aclose()
        except Exception as exc:
            print(f"Error registering outbound trunk: {exc}")

if __name__ == "__main__":
    asyncio.run(main())
