import os
from dotenv import load_dotenv

# Absolute path load_dotenv to avoid the "Silent Environment Trap" when run from different directories
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

import asyncio
import ssl
import certifi
import aiohttp
from livekit import api as lk_api

async def main():
    # Fetch environment variables
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    
    sip_domain = os.getenv("VOBIZ_SIP_DOMAIN")
    username = os.getenv("VOBIZ_SIP_USERNAME") or os.getenv("VOBIZ_USERNAME")
    password = os.getenv("VOBIZ_SIP_PASSWORD") or os.getenv("VOBIZ_PASSWORD")
    outbound_number = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    # Safely strip '+' prefix from outbound number if present
    if outbound_number and outbound_number.startswith("+"):
        outbound_number = outbound_number[1:]

    print("\n" + "="*50)
    print("--- PRE-FLIGHT DIAGNOSTICS ---")
    print(f"Env File Path:         {env_path}")
    print(f"LIVEKIT_URL:           {url}")
    print(f"LIVEKIT_API_KEY:       {key}")
    print(f"LIVEKIT_API_SECRET:    {'[LOADED]' if secret else '[MISSING]'}")
    print(f"VOBIZ_SIP_DOMAIN:      {sip_domain}")
    print(f"VOBIZ_SIP_USERNAME:    {username}")
    print(f"VOBIZ_SIP_PASSWORD:    Length = {len(password) if password else 0}")
    print(f"VOBIZ_OUTBOUND_NUMBER: {outbound_number}")
    print("="*50 + "\n")

    # Hard-Stop Validation
    required_vars = {
        "LIVEKIT_URL": url,
        "LIVEKIT_API_KEY": key,
        "LIVEKIT_API_SECRET": secret,
        "VOBIZ_SIP_DOMAIN": sip_domain,
        "VOBIZ_SIP_USERNAME (or VOBIZ_USERNAME)": username,
        "VOBIZ_SIP_PASSWORD (or VOBIZ_PASSWORD)": password,
        "VOBIZ_OUTBOUND_NUMBER": outbound_number,
    }
    
    missing_vars = [k for k, v in required_vars.items() if not v]
    if missing_vars:
        raise ValueError(
            f"CRITICAL: The following environment variables are missing or empty: {', '.join(missing_vars)}. "
            f"Please check your .env file at {env_path}"
        )

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
                name="vobiz-outbound-whitelisted",
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
