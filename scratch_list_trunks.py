import asyncio
import os
from dotenv import load_dotenv

load_dotenv(".env")

async def main():
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        print("Missing LiveKit credentials in .env")
        return

    import ssl
    import certifi
    import aiohttp
    from livekit import api as lk_api

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))

    try:
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        print("\nLISTING OUTBOUND TRUNKS:")
        outbound = await lk.sip.list_sip_outbound_trunk(lk_api.ListSIPOutboundTrunkRequest())
        print(outbound)
            
        print("\nLISTING INBOUND TRUNKS:")
        inbound = await lk.sip.list_sip_inbound_trunk(lk_api.ListSIPInboundTrunkRequest())
        print(inbound)

        await lk.aclose()
    except Exception as exc:
        print(f"Failed to list trunks: {exc}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
