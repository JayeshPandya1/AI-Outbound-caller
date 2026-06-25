import asyncio
import sys

# ── Windows Python 3.13 fix ──────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import json
import logging
import os
import ssl
import time
import certifi
from typing import Optional

from dotenv import load_dotenv
load_dotenv(".env", override=False)

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents.multimodal import MultimodalAgent
from livekit.plugins import silero
from google.genai import types as _gt

from db import init_db, log_error, get_enabled_tools, get_setting, log_call, update_call_outcome, SENSITIVE_KEYS
from prompts import build_prompt
from tools import AppointmentTools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

FALLBACK_TRUNK_ID: Optional[str] = None
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")

# ── Local VAD Configuration (Ignores SIP Static) ─────────────────────────────
custom_vad = silero.VAD.load(
    activation_threshold=0.7,   
    min_speech_duration=0.3,    
    min_silence_duration=0.6    
)

async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      
        logger.info(msg)
    elif level == "warning": 
        logger.warning(msg)
        try:
            asyncio.create_task(log_error("agent", msg, detail, level))
        except Exception:
            pass
    else:                    
        logger.error(msg)
        try:
            asyncio.create_task(log_error("agent", msg, detail, level))
        except Exception:
            pass

def load_db_settings_to_env() -> None:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            k = row.get("key")
            v = row.get("value")
            if v and k:
                if k in SENSITIVE_KEYS:
                    if not os.environ.get(k):
                        os.environ[k] = v
                else:
                    if not os.environ.get(k):
                        os.environ[k] = v
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)

_google_realtime = None
_google_beta_realtime = None
try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
    except AttributeError:
        pass
except ImportError:
    pass

# ── Multimodal Session Builder ───────────────────────────────────────────────
def _build_session(tool_ctx, system_prompt: str, gemini_model: str, gemini_voice: str) -> MultimodalAgent:
    voice_lower = gemini_voice.lower()
    male_voices = ["achird", "algenib", "algieba", "alnilam", "charon", "enceladus", "fenrir", "iapetus", "orus", "perseus", "puck", "rasalgethi", "sadachbia", "sadaltager", "schedar", "umbriel", "zubenelgenubi"]
    if voice_lower in male_voices:
        gemini_voice = "Fenrir" if voice_lower == "fenrir" else "Puck" if voice_lower == "puck" else "Charon"
    else:
        gemini_voice = "Kore" if voice_lower == "kore" else "Aoede"

    RealtimeClass = _google_realtime or _google_beta_realtime
    if RealtimeClass is None:
        raise RuntimeError("Gemini Live RealtimeModel could not be loaded.")

    logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
    
    # Cloud VAD explicitly disabled — relying entirely on local Silero VAD
    realtime_kwargs = dict(
        model=gemini_model, 
        voice=gemini_voice, 
        instructions=system_prompt,
        temperature=0.6,
        max_output_tokens=256,
        thinking_config=_gt.ThinkingConfig(thinking_level="minimal"),
    )
    
    llm_instance = RealtimeClass(**realtime_kwargs)

    return MultimodalAgent(
        model=llm_instance,
        turn_detector=custom_vad,
        fnc_ctx=tool_ctx 
    )

async def entrypoint(ctx: agents.JobContext) -> None:
    await _log("info", f"Job started — room: {ctx.room.name}")
    call_start_time = time.time()

    phone_number = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt = None
    voice_override = None
    model_override = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number   = data.get("phone_number")
            lead_name      = data.get("lead_name", lead_name)
            business_name  = data.get("business_name", business_name)
            service_type   = data.get("service_type", service_type)
            custom_prompt  = data.get("system_prompt")
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
        except Exception:
            pass

    _sip_identity = f"sip_{phone_number}" if phone_number else None
    call_db_id = None

    if phone_number:
        try:
            call_db_id = await log_call(
                phone_number=phone_number, lead_name=lead_name, outcome="initiated",
                reason="Preparing to dial", duration_seconds=0
            )
            from db import get_calls_by_phone, get_appointments_by_phone, get_contact_memory
            asyncio.create_task(get_calls_by_phone(phone_number))
            asyncio.create_task(get_appointments_by_phone(phone_number))
            asyncio.create_task(get_contact_memory(phone_number))
        except Exception:
            pass

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                  service_type=service_type, custom_prompt=custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name, call_db_id=call_db_id)

    async def _noop(): return None
    _model_coro = get_setting("GEMINI_MODEL", "gemini-3.1-flash-live-preview") if not model_override else _noop()
    _voice_coro = get_setting("GEMINI_TTS_VOICE", "Aoede") if not voice_override else _noop()
    _model_r, _voice_r = await asyncio.gather(_model_coro, _voice_coro)
    
    gemini_model = model_override or _model_r
    gemini_voice = voice_override or _voice_r

    await ctx.connect()
    
    # ── Dial SIP Participant ─────────────────────────────────────────────────
    target_participant = None
    if phone_number:
        trunk_id = FALLBACK_TRUNK_ID or await get_setting("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            ctx.shutdown()
            return
        
        _answered_event = asyncio.Event()
        _failed_event = asyncio.Event()

        def _on_track_subscribed(track, pub, participant):
            if participant.identity == _sip_identity and participant.attributes.get("sip.callStatus") == "active":
                _answered_event.set()

        def _on_attributes_changed(changed, participant):
            if participant.identity == _sip_identity:
                status = participant.attributes.get("sip.callStatus")
                if status == "active":
                    _answered_event.set()
                elif status == "hangup":
                    _failed_event.set()

        ctx.room.on("track_subscribed", _on_track_subscribed)
        ctx.room.on("participant_attributes_changed", _on_attributes_changed)
        
        outbound_number = await get_setting("VOBIZ_OUTBOUND_NUMBER")
        from datetime import timedelta
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name, sip_trunk_id=trunk_id, sip_call_to=phone_number,
                sip_number=outbound_number, participant_identity=_sip_identity,
                ringing_timeout=timedelta(seconds=30), wait_until_answered=False,
            )
        )

        try:
            done, pending = await asyncio.wait(
                [asyncio.create_task(_answered_event.wait()), asyncio.create_task(_failed_event.wait())],
                return_when=asyncio.FIRST_COMPLETED, timeout=60.0
            )
            for p in pending: p.cancel()
            if _failed_event.is_set() or not done:
                raise RuntimeError("Call failed or timed out")
        except Exception:
            ctx.shutdown()
            return
        finally:
            ctx.room.off("track_subscribed", _on_track_subscribed)
            ctx.room.off("participant_attributes_changed", _on_attributes_changed)

        for p in ctx.room.remote_participants.values():
            if p.identity == _sip_identity:
                target_participant = p
                break
                
        tool_ctx._call_start_time = time.time()

    # ── Start Multimodal Agent ───────────────────────────────────────────────
    agent = _build_session(tool_ctx, system_prompt, gemini_model, gemini_voice)

    @agent.on("user_started_speaking")
    def _on_user_started():
        logger.info("[VAD DETECT] Silero VAD: User started speaking")

    @agent.on("user_stopped_speaking")
    def _on_user_stopped():
        logger.info("[VAD DETECT] Silero VAD: User stopped speaking")

    @agent.on("agent_started_speaking")
    def _on_agent_started():
        logger.info("[AGENT STATE] Agent started speaking")

    @agent.on("agent_stopped_speaking")
    def _on_agent_stopped():
        logger.info("[AGENT STATE] Agent stopped speaking")

    if target_participant:
        agent.start(ctx.room, target_participant)
        await asyncio.sleep(1.0)
        try:
            agent.generate_reply()
        except Exception as e:
            logger.error(f"Failed to trigger greeting: {e}")

    # Keep alive until disconnect
    if phone_number:
        _disconnect_event = asyncio.Event()
        def _on_participant_disconnected(participant):
            if participant.identity == _sip_identity: _disconnect_event.set()
        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", lambda: _disconnect_event.set())

        await _disconnect_event.wait()
        
        if getattr(tool_ctx, "call_active", True):
            duration = int(time.time() - tool_ctx._call_start_time)
            if call_db_id:
                await update_call_outcome(call_db_id, "dropped", "Lead hung up", duration)
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        await _done.wait()

if __name__ == "__main__":
    load_db_settings_to_env()
    init_db()
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller"))
