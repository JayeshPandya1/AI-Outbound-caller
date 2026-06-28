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
        
        print("\nLISTING ACTIVE ROOMS:")
        rooms_res = await lk.room.list_rooms(lk_api.ListRoomsRequest())
        rooms = rooms_res.rooms
        print(f"Found {len(rooms)} active rooms.")
        for room in rooms:
            print(f"Room: {room.name} (SID: {room.sid}) | Participants: {room.num_participants}")
            try:
                print(f"Deleting room {room.name}...")
                await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room.name))
                print("Deleted successfully.")
            except Exception as e:
                print(f"Could not delete room {room.name}: {e}")
            
        await lk.aclose()
    except Exception as exc:
        print(f"Failed: {exc}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
