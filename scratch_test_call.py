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
    outbound_number = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    to_number = sys.argv[1] if len(sys.argv) > 1 else "+919479834133"
    
    if not all([url, key, secret, trunk_id, outbound_number]):
        print("Missing credentials in .env")
        return

    import ssl
    import certifi
    import aiohttp
    from datetime import timedelta
    from livekit import api as lk_api
    from livekit import rtc

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))

    try:
        # We will connect a Room client to the room to listen to events!
        room_name = f"test-dial-{int(time.time())}"
        grants = lk_api.VideoGrants(room_join=True, room=room_name)
        token = (
            lk_api.AccessToken(key, secret)
            .with_identity("test-listener")
            .with_grants(grants)
            .to_jwt()
        )
        
        print(f"Connecting to room: {room_name}...")
        room = rtc.Room()
        
        _sip_identity = f"sip_{to_number}"
        
        @room.on("participant_connected")
        def _on_p_conn(p: rtc.RemoteParticipant):
            print(f"[EVENT] Participant connected: {p.identity} | Attributes: {p.attributes}")
            
        @room.on("participant_disconnected")
        def _on_p_disc(p: rtc.RemoteParticipant):
            print(f"[EVENT] Participant disconnected: {p.identity}")
            
        @room.on("track_subscribed")
        def _on_track_sub(track, pub, p):
            print(f"[EVENT] Track subscribed for {p.identity} | Kind: {track.kind} | Attributes: {p.attributes}")
            
        @room.on("participant_attributes_changed")
        def _on_attr_changed(changed, p):
            print(f"[EVENT] Attributes changed for {p.identity}: {changed} | Current attributes: {p.attributes}")

        await room.connect(url, token)
        print("Connected to room successfully. Initiating dial...")
        
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        # Test dialing with wait_until_answered=True
        req = lk_api.CreateSIPParticipantRequest(
            room_name=room_name,
            sip_trunk_id=trunk_id,
            sip_call_to=to_number,
            # Test without passing sip_number first
            participant_identity=_sip_identity,
            ringing_timeout=timedelta(seconds=30),
            wait_until_answered=True,
        )
        
        print("Creating SIP participant (wait_until_answered=False)...")
        await lk.sip.create_sip_participant(req)
        print("SIP participant creation command sent. Listening for events for 15 seconds...")
        
        # Wait and print any events
        await asyncio.sleep(15.0)
        
        print("Disconnecting room...")
        await room.disconnect()
        await lk.aclose()
    except Exception as exc:
        print(f"FAILED: {exc}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
