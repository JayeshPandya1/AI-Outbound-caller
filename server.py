"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import ssl
import certifi
import aiohttp
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_calls_by_phone, get_campaign,
    get_contacts, get_errors, get_logs, get_setting, get_stats, init_db, log_error,
    save_settings, set_setting, update_call_notes, update_call_recording, update_campaign_run_stats, update_campaign_status,
    delete_campaign,
    ensure_default_user, get_user_by_login_id, verify_password, create_user_session,
    get_user_session, delete_user_session, get_all_active_sessions_for_user,
    delete_user_session_by_id, update_user
)
from prompts import DEFAULT_SYSTEM_PROMPT

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Authentication Middleware ──────────────────────────────────────────────────

@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    exempt_paths = [
        "/login",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/vobiz/webhook",
    ]
    
    path = request.url.path
    if path.startswith("/api/") and path not in exempt_paths:
        session_id = request.cookies.get("session_id")
        if not session_id:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        
        session = await get_user_session(session_id)
        if not session or not session.get("users") or not session["users"].get("is_active"):
            return JSONResponse(status_code=401, content={"detail": "Session expired or invalid"})
        
        request.state.user = session["users"]
        request.state.session_id = session_id
        
    response = await call_next(request)
    return response


@app.on_event("startup")
async def _startup():
    await ensure_default_user()
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


# ── Request models ────────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-2.5-flash-native-audio-preview-12-2025"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


# ── Dashboard & Authentication ──────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def api_auth_login(req: LoginRequest, request: Request):
    username = req.username.strip()
    password = req.password
    
    user = await get_user_by_login_id(username)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid username or password.")
    
    from datetime import datetime, timezone
    if user.get("locked_until"):
        locked_until = datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00"))
        if locked_until > datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Account is temporarily locked. Please try again in 15 minutes.")
    
    is_valid = verify_password(password, user["password_hash"])
    if not is_valid:
        failed_count = user.get("failed_login_count", 0) + 1
        updates = {"failed_login_count": failed_count}
        if failed_count >= 5:
            from datetime import timedelta
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
            updates["locked_until"] = locked_until
            await update_user(user["id"], updates)
            raise HTTPException(status_code=400, detail="Account is temporarily locked due to too many failed attempts.")
        else:
            await update_user(user["id"], updates)
            raise HTTPException(status_code=400, detail="Invalid username or password.")
            
    if not user.get("is_active", True):
        raise HTTPException(status_code=400, detail="Account is inactive. Contact administrator.")
        
    await update_user(user["id"], {
        "failed_login_count": 0,
        "locked_until": None,
        "last_login_at": datetime.now(timezone.utc).isoformat()
    })
    
    import secrets
    session_token = secrets.token_hex(32)
    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None
    
    await create_user_session(
        user_id=user["id"],
        session_token=session_token,
        user_agent=user_agent,
        ip_address=ip_address
    )
    
    response = JSONResponse(content={"status": "success", "username": user["login_id"]})
    response.set_cookie(
        key="session_id",
        value=session_token,
        httponly=True,
        max_age=86400,  # 24 hours
        samesite="lax",
        secure=False  # Set to True over HTTPS in production
    )
    return response


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        await delete_user_session(session_id)
        
    response = JSONResponse(content={"status": "success"})
    response.delete_cookie(key="session_id")
    return response


@app.get("/api/auth/session")
async def api_auth_session(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = await get_user_session(session_id)
    if not session or not session.get("users") or not session["users"].get("is_active"):
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return {
        "authenticated": True,
        "username": session["users"]["login_id"]
    }


@app.get("/api/auth/sessions")
async def api_auth_get_sessions(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = await get_user_session(session_id)
    if not session or not session.get("users") or not session["users"].get("is_active"):
        raise HTTPException(status_code=401, detail="Session expired or invalid")
        
    user_id = session["users"]["id"]
    sessions = await get_all_active_sessions_for_user(user_id)
    
    formatted_sessions = []
    for s in sessions:
        formatted_sessions.append({
            "id": s["id"],
            "device": s["user_agent"] or "Unknown Device",
            "ip": s["ip_address"] or "Unknown IP",
            "lastActive": "Active now" if s["session_token"] == session_id else s["last_active"],
            "current": s["session_token"] == session_id
        })
    return formatted_sessions


@app.delete("/api/auth/sessions/{session_db_id}")
async def api_auth_revoke_session(session_db_id: str, request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = await get_user_session(session_id)
    if not session or not session.get("users") or not session["users"].get("is_active"):
        raise HTTPException(status_code=401, detail="Session expired or invalid")
        
    user_id = session["users"]["id"]
    user_sessions = await get_all_active_sessions_for_user(user_id)
    session_ids = [s["id"] for s in user_sessions]
    
    if session_db_id in session_ids:
        await delete_user_session_by_id(session_db_id)
        return {"status": "success", "message": "Session revoked."}
    else:
        raise HTTPException(status_code=404, detail="Session not found or unauthorized.")


@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        session = await get_user_session(session_id)
        if session and session.get("users") and session["users"].get("is_active"):
            return HTMLResponse(content="<script>window.location.href='/';</script>")
            
    login_path = Path(__file__).parent / "ui" / "login.html"
    if login_path.exists():
        return HTMLResponse(content=login_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Login Page not found — place login.html in ui/</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    session_id = request.cookies.get("session_id")
    is_authenticated = False
    if session_id:
        session = await get_user_session(session_id)
        if session and session.get("users") and session["users"].get("is_active"):
            is_authenticated = True
            
    if is_authenticated:
        html_path = Path(__file__).parent / "ui" / "index.html"
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)
    else:
        # Serve login page directly at "/" when unauthenticated
        login_path = Path(__file__).parent / "ui" / "login.html"
        if login_path.exists():
            return HTMLResponse(content=login_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Login Page not found — place login.html in ui/</h1>", status_code=404)


# ── Call dispatch ─────────────────────────────────────────────────────────────

@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    t0 = time.time()
    url, key, secret = await asyncio.gather(
        eff("LIVEKIT_URL"),
        eff("LIVEKIT_API_KEY"),
        eff("LIVEKIT_API_SECRET")
    )

    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings → LiveKit.")

    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")

    effective_prompt = req.system_prompt
    effective_voice = None
    effective_model = None
    effective_tools = None

    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")

    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata: dict = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "system_prompt": effective_prompt,
    }
    if effective_voice:  metadata["voice_override"] = effective_voice
    if effective_model:  metadata["model_override"] = effective_model
    if effective_tools:  metadata["tools_override"] = effective_tools

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as session:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            
            # 1. Prevent concurrent active calls
            rooms_res = await lk.room.list_rooms(lk_api.ListRoomsRequest())
            active_calls = [r for r in rooms_res.rooms if r.name.startswith("call-") or r.name.startswith("camp-")]
            if active_calls:
                await lk.aclose()
                raise HTTPException(400, "A call is currently in progress. Please wait for the current call to finish.")
                
            # 2. Create room and dispatch
            await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
            await lk.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(
                    agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
                )
            )
            await lk.aclose()
            
        logger.info(f"[LATENCY AUDIT] Call dispatch completed in {time.time() - t0:.2f}s for {phone}")
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(500, f"Dispatch failed: {exc}")


@app.get("/api/active-rooms")
async def api_get_active_rooms():
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")

    if not all([url, key, secret]):
        return {"rooms": []}

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as session:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            rooms_res = await lk.room.list_rooms(lk_api.ListRoomsRequest())
            rooms = [r.name for r in rooms_res.rooms]
            await lk.aclose()
            return {"rooms": rooms}
    except Exception as e:
        logger.error("Failed to list active rooms: %s", e)
        return {"rooms": []}


# ── Calls ─────────────────────────────────────────────────────────────────────

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Prompt ────────────────────────────────────────────────────────────────────

@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        os.environ[k] = str(v)
    return {"status": "saved", "count": len(filtered)}


# ── SIP trunk setup ───────────────────────────────────────────────────────────

@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username   = await eff("VOBIZ_USERNAME")
    password   = await eff("VOBIZ_PASSWORD")
    phone      = await eff("VOBIZ_OUTBOUND_NUMBER")

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz credentials in Settings first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=[phone],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await lk.aclose()
        await session.close()
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# ── CRM ───────────────────────────────────────────────────────────────────────

@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


async def fetch_vobiz_recording_background(
    call_id: str,
    call_uuid: Optional[str],
    sip_call_id: Optional[str],
    phone_clean: str,
    duration: Optional[int]
):
    # Wait 120 seconds (2 minutes) to ensure Vobiz has fully processed and saved the recording
    await asyncio.sleep(120)
    
    try:
        auth_id = await get_setting("VOBIZ_AUTH_ID") or os.getenv("VOBIZ_AUTH_ID")
        auth_token = await get_setting("VOBIZ_AUTH_TOKEN") or os.getenv("VOBIZ_AUTH_TOKEN")
        if not auth_id or not auth_token:
            await log_error(
                "vobiz_webhook_bg",
                f"Cannot fetch recording for call {call_id}: VOBIZ_AUTH_ID or VOBIZ_AUTH_TOKEN not configured",
                level="warning"
            )
            return

        url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Recording/"
        headers = {
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Accept": "application/json"
        }

        max_attempts = 3
        retry_delay = 10
        matched_rec = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await asyncio.sleep(retry_delay)

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=15) as resp:
                        if resp.status != 200:
                            resp_text = await resp.text()
                            await log_error(
                                "vobiz_webhook_bg",
                                f"Attempt {attempt}: Failed to fetch Vobiz recordings list (Status {resp.status})",
                                detail=resp_text,
                                level="warning"
                            )
                            continue
                        recordings = await resp.json()
            except Exception as conn_err:
                await log_error(
                    "vobiz_webhook_bg",
                    f"Attempt {attempt}: Connection error querying Vobiz API: {conn_err}",
                    level="warning"
                )
                continue

            recordings_list = recordings.get("objects") if isinstance(recordings, dict) else recordings
            if not isinstance(recordings_list, list):
                await log_error(
                    "vobiz_webhook_bg",
                    f"Attempt {attempt}: Invalid recordings payload format (expected objects list key)",
                    detail=json.dumps(recordings),
                    level="warning"
                )
                continue

            # Match by call_uuid or sip_call_id
            for r in recordings_list:
                r_call_uuid = r.get("call_uuid") or r.get("CallUUID") or r.get("sip_call_id") or r.get("SIPCallID") or r.get("recording_id")
                if (call_uuid and r_call_uuid == call_uuid) or (sip_call_id and r_call_uuid == sip_call_id):
                    matched_rec = r
                    break

            if matched_rec:
                break

            await log_error(
                "vobiz_webhook_bg",
                f"Attempt {attempt}: Recording not found yet in Vobiz database for call_uuid: {call_uuid} / sip_call_id: {sip_call_id}. Retrying...",
                detail=json.dumps({"recordings_found": len(recordings_list)}),
                level="info"
            )

        if not matched_rec:
            await log_error(
                "vobiz_webhook_bg",
                f"Failed: Recording not found in Vobiz database after {max_attempts} attempts for call_uuid {call_uuid} / sip_call_id {sip_call_id}",
                level="warning"
            )
            return

        rec_url = matched_rec.get("recording_url") or matched_rec.get("recording") or matched_rec.get("audio_url")
        if rec_url:
            ok = await update_call_recording(call_id, rec_url, duration)
            if ok:
                await log_error(
                    "vobiz_webhook_bg",
                    f"Success: Located and saved recording for call {call_id} from Vobiz API",
                    detail=f"Secure URL: {rec_url}",
                    level="info"
                )
            else:
                await log_error(
                    "vobiz_webhook_bg",
                    f"Failed to update call {call_id} with recording URL in database",
                    level="error"
                )
        else:
            await log_error(
                "vobiz_webhook_bg",
                "Matched Vobiz recording object missing recording_url field",
                detail=json.dumps(matched_rec),
                level="warning"
            )

    except Exception as bg_err:
        await log_error(
            "vobiz_webhook_bg",
            f"Exception in background recording lookup: {bg_err}",
            level="error"
        )


@app.post("/api/vobiz/webhook")
async def api_vobiz_webhook(req: Request, secret: Optional[str] = Query(None)):
    # 1. Verify webhook secret key for security (if configured)
    expected_secret = await get_setting("VOBIZ_WEBHOOK_SECRET") or os.getenv("VOBIZ_WEBHOOK_SECRET")
    if expected_secret and secret != expected_secret:
        await log_error(
            "vobiz_webhook",
            f"Unauthorized webhook attempt: secret mismatch (got {secret})",
            level="warning"
        )
        raise HTTPException(401, "Unauthorized: Invalid webhook secret")

    # 2. Parse payload: Support both JSON and Form Urlencoded
    payload = {}
    content_type = req.headers.get("content-type", "")
    
    try:
        if "application/x-www-form-urlencoded" in content_type:
            form_data = await req.form()
            payload = dict(form_data)
            await log_error(
                "vobiz_webhook",
                "Received form-encoded webhook payload",
                detail=json.dumps(payload),
                level="info"
            )
        else:
            # Default to JSON parsing, fallback to form parsing on error
            try:
                payload = await req.json()
                await log_error(
                    "vobiz_webhook",
                    "Received JSON webhook payload",
                    detail=json.dumps(payload),
                    level="info"
                )
            except Exception:
                form_data = await req.form()
                if form_data:
                    payload = dict(form_data)
                    await log_error(
                        "vobiz_webhook",
                        "Received form-encoded payload (fallback due to JSON parse failure)",
                        detail=json.dumps(payload),
                        level="info"
                    )
                else:
                    raise
    except Exception as parse_err:
        await log_error(
            "vobiz_webhook",
            f"Failed to parse webhook payload: {parse_err}",
            level="error"
        )
        raise HTTPException(400, f"Invalid payload format: {parse_err}")

    # 3. Extract variables with case-insensitivity
    def get_val_case_insensitive(d, keys_list):
        for k in keys_list:
            if k in d:
                return d[k]
            # Case-insensitive check
            for dict_key, dict_val in d.items():
                if dict_key.lower() == k.lower():
                    return dict_val
        return None

    phone = get_val_case_insensitive(payload, ["destination_number", "to", "phone_number", "phone", "dest_number", "destination"])
    rec_url = get_val_case_insensitive(payload, ["recording_url", "recording", "audio_url", "rec_url", "recording_link", "recordingurl", "record_url"])
    duration = get_val_case_insensitive(payload, ["duration", "duration_seconds", "billsec", "duration_sec"])

    if duration:
        try:
            duration = int(duration)
        except ValueError:
            duration = None

    if not phone:
        await log_error(
            "vobiz_webhook",
            "Webhook ignored: Missing destination phone number in payload",
            detail=json.dumps(payload),
            level="warning"
        )
        return {"status": "ignored", "reason": "missing phone number"}

    # Normalize phone number prefix
    phone_clean = phone.strip()
    if not phone_clean.startswith("+"):
        if len(phone_clean) == 10:
            phone_clean = f"+91{phone_clean}"
        elif len(phone_clean) == 12 and phone_clean.startswith("91"):
            phone_clean = f"+{phone_clean}"

    # 4. Find the matching call log in database
    try:
        calls = await get_calls_by_phone(phone_clean)
        if not calls and phone_clean != phone:
            calls = await get_calls_by_phone(phone)

        if not calls:
            await log_error(
                "vobiz_webhook",
                f"Webhook ignored: No call logs found in Supabase matching phone: {phone_clean}",
                detail=json.dumps(payload),
                level="warning"
            )
            return {"status": "ignored", "reason": "no matching call log found"}

        # Find the latest call log (most recent)
        latest_call = calls[0]
        
        # Verify call log is fresh (within 10 minutes)
        log_time_str = latest_call.get("timestamp")
        is_fresh = True
        diff_seconds = 0
        if log_time_str:
            try:
                from datetime import datetime, timezone
                log_time = datetime.fromisoformat(log_time_str.replace("Z", "+00:00"))
                diff_seconds = (datetime.now(timezone.utc) - log_time).total_seconds()
                if diff_seconds > 600:
                    is_fresh = False
            except Exception as t_err:
                logger.warning("Failed to parse log time for validation: %s", t_err)

        if not is_fresh:
            await log_error(
                "vobiz_webhook",
                f"Webhook ignored: Latest call log for {phone_clean} is stale ({diff_seconds:.1f} seconds ago)",
                detail=json.dumps({"payload": payload, "latest_call": latest_call}),
                level="warning"
            )
            return {"status": "ignored", "reason": "matching log is stale"}

        # 5. Update call record with Vobiz's link and actual duration
        call_id = latest_call["id"]
        if rec_url:
            ok = await update_call_recording(call_id, rec_url, duration)
            if ok:
                await log_error(
                    "vobiz_webhook",
                    f"Success: Updated call {call_id} ({phone_clean}) with Vobiz recording",
                    detail=f"URL: {rec_url}, Duration: {duration}s",
                    level="info"
                )
                return {"status": "updated", "call_id": call_id}
            else:
                await log_error(
                    "vobiz_webhook",
                    f"Failed to update call recording for ID {call_id} in database",
                    level="error"
                )
                raise HTTPException(500, "Database update failed")
        else:
            # If recording url is missing from Hangup payload, check if we can query the Recording API in background
            auth_id = await get_setting("VOBIZ_AUTH_ID") or os.getenv("VOBIZ_AUTH_ID")
            auth_token = await get_setting("VOBIZ_AUTH_TOKEN") or os.getenv("VOBIZ_AUTH_TOKEN")
            call_uuid = get_val_case_insensitive(payload, ["call_uuid", "calluuid"])
            sip_call_id = get_val_case_insensitive(payload, ["sip_call_id", "sipcallid", "sipcall_id"])
            
            if auth_id and auth_token and (call_uuid or sip_call_id):
                asyncio.create_task(
                    fetch_vobiz_recording_background(call_id, call_uuid, sip_call_id, phone_clean, duration)
                )
                await log_error(
                    "vobiz_webhook",
                    f"Call ended for {phone_clean}. Queued background recording API check.",
                    detail=json.dumps({"call_uuid": call_uuid, "sip_call_id": sip_call_id}),
                    level="info"
                )
                return {
                    "status": "queued_recording_lookup",
                    "call_id": call_id,
                    "call_uuid": call_uuid,
                    "sip_call_id": sip_call_id
                }
            else:
                await log_error(
                    "vobiz_webhook",
                    "Webhook ignored: Missing recording URL in payload, and Vobiz API credentials or CallUUID/SIPCallID is missing",
                    detail=json.dumps(payload),
                    level="warning"
                )
                return {"status": "ignored", "reason": "missing recording URL in payload and credentials"}

    except Exception as e:
        await log_error(
            "vobiz_webhook",
            f"Error processing webhook: {e}",
            detail=json.dumps(payload),
            level="error"
        )
        raise HTTPException(500, str(e))


@app.get("/api/vobiz/stream-recording")
async def api_vobiz_stream_recording(request: Request, url: str = Query(...)):
    parsed_url = url.strip()
    if not any(domain in parsed_url for domain in ["vobiz.ai", "vobiz.com"]):
        raise HTTPException(400, "Forbidden: Only Vobiz recording URLs can be proxied")

    auth_id = await get_setting("VOBIZ_AUTH_ID") or os.getenv("VOBIZ_AUTH_ID")
    auth_token = await get_setting("VOBIZ_AUTH_TOKEN") or os.getenv("VOBIZ_AUTH_TOKEN")
    if not auth_id or not auth_token:
        raise HTTPException(400, "Vobiz API credentials are not configured on the server")

    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token
    }

    # Forward the Range header if requested by client (e.g. iOS Safari)
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    try:
        session = aiohttp.ClientSession()
        resp = await session.get(parsed_url, headers=headers)
        status_code = resp.status
        
        # Extract response headers to forward range/seek info to browser
        response_headers = {
            "Accept-Ranges": resp.headers.get("Accept-Ranges") or "bytes"
        }
        if resp.headers.get("Content-Range"):
            response_headers["Content-Range"] = resp.headers.get("Content-Range")
        if resp.headers.get("Content-Length"):
            response_headers["Content-Length"] = resp.headers.get("Content-Length")
            
        content_type = resp.headers.get("Content-Type") or "audio/mpeg"
        if parsed_url.endswith(".wav"):
            content_type = "audio/wav"
        elif parsed_url.endswith(".ogg"):
            content_type = "audio/ogg"

        async def stream_generator():
            try:
                if status_code >= 400:
                    yield b"Failed to retrieve recording from Vobiz secure storage"
                    return
                async for chunk, _ in resp.content.iter_chunks():
                    yield chunk
            finally:
                resp.close()
                await session.close()

        return StreamingResponse(
            stream_generator(),
            status_code=status_code,
            headers=response_headers,
            media_type=content_type
        )
    except Exception as e:
        logger.error("Error setting up Vobiz recording stream proxy: %s", e)
        raise HTTPException(500, f"Error starting recording stream: {e}")



# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools, is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def _dispatch_one(lk, lk_api, contact: dict, room_name: str,
                         prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None
        metadata: dict = {
            "phone_number": contact["phone"],
            "lead_name": contact.get("lead_name", "there"),
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": saved_prompt,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):   metadata["voice_override"] = profile["voice"]
            if profile.get("model"):   metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"): metadata["tools_override"] = profile["enabled_tools"]
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata))
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay = int(campaign.get("call_delay_seconds") or 3)
    prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            phone = contact.get("phone", "")
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            success = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile)
            if success:
                ok_count += 1
                try:
                    # Sequential loop: wait for room to start and then wait for it to close
                    room_started = False
                    for _ in range(10):
                        rooms_res = await lk.room.list_rooms(lk_api_module.ListRoomsRequest())
                        if any(r.name == room_name for r in rooms_res.rooms):
                            room_started = True
                            break
                        await asyncio.sleep(1)
                    
                    if room_started:
                        while True:
                            rooms_res = await lk.room.list_rooms(lk_api_module.ListRoomsRequest())
                            if not any(r.name == room_name for r in rooms_res.rooms):
                                break
                            await asyncio.sleep(2)
                except Exception as wait_exc:
                    logger.warning("Error waiting for campaign room %s to close: %s", room_name, wait_exc)
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(delay)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
    finally:
        await session.close()

    await update_campaign_run_stats(campaign_id, ok_count, fail_count)
    logger.info("Campaign %s done — %d dispatched, %d failed", campaign_id, ok_count, fail_count)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds, system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}
