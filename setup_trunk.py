"""
Run once: registers Vobiz SIP credentials with LiveKit and prints the ST_xxx trunk ID.
Copy the printed trunk ID into your .env file as OUTBOUND_TRUNK_ID.

Usage:  python setup_trunk.py
"""

import asyncio, os, ssl, aiohttp
import certifi
from dotenv import load_dotenv

load_dotenv(".env")

LIVEKIT_URL    = os.getenv("LIVEKIT_URL", "")
LIVEKIT_KEY    = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
SIP_DOMAIN     = os.getenv("VOBIZ_SIP_DOMAIN", "")
SIP_USER       = os.getenv("VOBIZ_USERNAME", "")
SIP_PASS       = os.getenv("VOBIZ_PASSWORD", "")
PHONE          = os.getenv("VOBIZ_OUTBOUND_NUMBER", "")

async def main():
    if not all([LIVEKIT_URL, LIVEKIT_KEY, LIVEKIT_SECRET, SIP_DOMAIN, SIP_USER, SIP_PASS, PHONE]):
        print("ERROR: Missing required env vars. Check .env file.")
        return

    from livekit import api as lk_api

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))

    try:
        lk = lk_api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_KEY, api_secret=LIVEKIT_SECRET, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=SIP_DOMAIN,
                    auth_username=SIP_USER,
                    auth_password=SIP_PASS,
                    numbers=[PHONE],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        print(f"\n{'='*60}")
        print(f"SUCCESS! LiveKit SIP Trunk created.")
        print(f"Trunk ID: {trunk_id}")
        print(f"{'='*60}")
        print(f"\nNow update your .env file:")
        print(f"  OUTBOUND_TRUNK_ID={trunk_id}")
        print(f"\nOr it is already saved to Supabase settings table.")
        print(f"{'='*60}\n")

        # Also persist to Supabase settings
        try:
            from supabase import create_client
            sb = create_client(os.getenv("SUPABASE_URL",""), os.getenv("SUPABASE_SERVICE_KEY",""))
            sb.table("settings").upsert({"key": "OUTBOUND_TRUNK_ID", "value": trunk_id}, on_conflict="key").execute()
            print("Also saved to Supabase settings table.")
        except Exception as e:
            print(f"Could not save to Supabase: {e}")

        await lk.aclose()
    except Exception as exc:
        print(f"FAILED: {exc}")
    finally:
        await session.close()

asyncio.run(main())
