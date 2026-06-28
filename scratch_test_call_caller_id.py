import asyncio
import os
import sys
import time
from dotenv import load_dotenv

load_dotenv(".env")

async def main():
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
    
    to_number = sys.argv[1] if len(sys.argv) > 1 else "+919479834133"
    
    if not all([url, key, secret, trunk_id]):
        print("Missing credentials in .env")
        return

    import ssl
    import certifi
    import aiohttp
    from datetime import timedelta
    from livekit import api as lk_api

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))

    formats = [
        ("No sip_number (default)", None),
        ("10-digit number (8071583188)", "8071583188"),
        ("12-digit number (918071583188)", "918071583188"),
        ("E.164 (+918071583188)", "+918071583188"),
    ]

    try:
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        for name, caller_id in formats:
            # First, delete any active rooms to start clean
            rooms_res = await lk.room.list_rooms(lk_api.ListRoomsRequest())
            for room in rooms_res.rooms:
                try:
                    await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room.name))
                except Exception:
                    pass
            await asyncio.sleep(1.0)
            
            room_name = f"test-callerid-{int(time.time())}"
            print(f"\n--- Testing Caller ID format: {name} ---")
            print(f"Room: {room_name}")
            
            req_params = {
                "room_name": room_name,
                "sip_trunk_id": trunk_id,
                "sip_call_to": to_number,
                "participant_identity": f"sip_{to_number.replace('+', '')}",
                "ringing_timeout": timedelta(seconds=15),
                "wait_until_answered": True,
            }
            if caller_id is not None:
                req_params["sip_number"] = caller_id
                
            req = lk_api.CreateSIPParticipantRequest(**req_params)
            
            t_start = time.time()
            try:
                print(f"Calling create_sip_participant (sip_number={caller_id})...")
                await lk.sip.create_sip_participant(req)
                print(f"SUCCESS! Call connected successfully in {time.time() - t_start:.2f}s")
                # Wait 5 seconds to let them hear or hang up
                await asyncio.sleep(5.0)
                break  # If success, stop testing other formats
            except Exception as exc:
                print(f"FAILED in {time.time() - t_start:.2f}s: {exc}")
                
        await lk.aclose()
    except Exception as exc:
        print(f"Global Failure: {exc}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
