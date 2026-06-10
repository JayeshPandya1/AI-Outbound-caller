import asyncio
import sys

# ── Windows Python 3.13 fix ──────────────────────────────────────────────────
# The default Proactor event loop on Windows causes AssertionError in
# livekit-agents worker.aclose(). Force the SelectorEventLoop instead.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv
load_dotenv(".env", override=False)  # MUST be first — before any module that reads os.getenv at import time

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation
from google.genai import types as _gt

from db import init_db, log_error, get_enabled_tools, get_setting
from prompts import build_prompt
from tools import AppointmentTools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        asyncio.create_task(log_error("agent", msg, detail, level))
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str, gemini_model: str, gemini_voice: str) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    CRITICAL SILENCE-PREVENTION CONFIG — all 3 required:
    1. SessionResumptionConfig(transparent=True) → auto-reconnects after timeout
    2. ContextWindowCompressionConfig → sliding window prevents token limit freeze
    3. RealtimeInputConfig(END_SENSITIVITY_LOW) → less aggressive VAD, 2s silence threshold

    ⚠️ EndSensitivity MUST use full string form: END_SENSITIVITY_LOW (not .LOW — AttributeError!)
    """

    # Map to supported Gemini Live voices (Aoede, Charon, Fenrir, Kore, Puck)
    voice_lower = gemini_voice.lower()
    male_voices = ["achird", "algenib", "algieba", "alnilam", "charon", "enceladus", "fenrir", "iapetus", "orus", "perseus", "puck", "rasalgethi", "sadachbia", "sadaltager", "schedar", "umbriel", "zubenelgenubi"]
    if voice_lower in male_voices:
        if voice_lower == "fenrir":
            gemini_voice = "Fenrir"
        elif voice_lower == "puck":
            gemini_voice = "Puck"
        else:
            gemini_voice = "Charon"
    else:
        if voice_lower == "kore":
            gemini_voice = "Kore"
        else:
            gemini_voice = "Aoede"

    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=1000,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied (VAD LOW, transparent resumption, context compression)")
        except Exception as _cfg_err:
            logger.warning("Could not build silence-prevention config: %s", _cfg_err)
            _realtime_input_cfg = None
            _session_resumption_cfg = None
            _ctx_compression_cfg = None

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    return AgentSession(stt=stt, llm=_google_llm(model="gemini-2.0-flash"), tts=tts, vad=None, tools=tools)


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


async def entrypoint(ctx: agents.JobContext) -> None:
    """
    Main entrypoint. Called per job. Reads metadata JSON from ctx.job.metadata.

    DIAL-FIRST PATTERN — CRITICAL:
    Start Gemini Live ONLY after create_sip_participant(wait_until_answered=True) completes.
    If you start the session during ring time (~20-30s), the Gemini idle timeout fires
    and the session dies silently before the call is even answered.

    NO close_on_disconnect — SIP legs have brief audio dropouts that look like disconnects.
    Instead, watch participant_disconnected event for the specific SIP identity.
    """
    await _log("info", f"Job started — room: {ctx.room.name}")

    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None

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
            tools_override = data.get("tools_override")
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    await _log("info", f"Call job received — phone={phone_number} lead={lead_name} biz={business_name}")

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                  service_type=service_type, custom_prompt=custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)

    gemini_model = model_override or await get_setting("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = voice_override or await get_setting("GEMINI_TTS_VOICE", "Aoede")

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # ── Connect ──────────────────────────────────────────────────────────────
    import time
    call_start_time = time.time()
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Dial — MUST come before session.start() ──────────────────────────────
    if phone_number:
        trunk_id = await get_setting("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            t_dial_start = time.time()
            await _log("info", f"[LATENCY AUDIT] Outbound call initiated to {phone_number} at {t_dial_start - call_start_time:.2f}s")
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            await _log("info", f"[LATENCY AUDIT] Callee answered. Ringing/pickup duration: {time.time() - t_dial_start:.2f}s")
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            try:
                from db import log_call
                await log_call(
                    phone_number=phone_number,
                    lead_name=lead_name,
                    outcome="no_answer",
                    reason=f"SIP dial failed: {str(exc)}",
                    duration_seconds=0
                )
            except Exception as log_exc:
                logger.error("Failed to log failed SIP dial: %s", log_exc)
            ctx.shutdown()
            return
        await _log("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")
        tool_ctx._call_start_time = time.time()

    # ── Build and start Gemini Live ──────────────────────────────────────────
    t_session_init = time.time()
    await _log("info", f"[LATENCY AUDIT] Building AI session (model={gemini_model}) at {t_session_init - call_start_time:.2f}s")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(tools=active_tools, system_prompt=system_prompt, gemini_model=gemini_model, gemini_voice=gemini_voice)
    await _log("info", f"[LATENCY AUDIT] Session object built in {time.time() - t_session_init:.2f}s")

    @session.on("generation_created")
    def _on_generation_created(event):
        logger.info(f"[LATENCY AUDIT] Model response generation started (id={event.response_id}) at {time.time() - call_start_time:.2f}s")

    @session.on("input_audio_transcription_completed")
    def _on_input_audio_transcription_completed(event):
        logger.info(f"[LATENCY AUDIT] User utterance transcription finished: '{event.transcript}' at {time.time() - call_start_time:.2f}s (is_final={event.is_final})")

    @session.on("input_speech_started")
    def _on_input_speech_started(event):
        logger.info(f"[LATENCY AUDIT] Voice Activity Detector (VAD): User started speaking at {time.time() - call_start_time:.2f}s")

    @session.on("input_speech_stopped")
    def _on_input_speech_stopped(event):
        logger.info(f"[LATENCY AUDIT] Voice Activity Detector (VAD): User stopped speaking at {time.time() - call_start_time:.2f}s")

    # Pass RoomInputOptions with noise cancellation and disable close_on_disconnect
    _room_input_options = RoomInputOptions(
        close_on_disconnect=False,
        noise_cancellation=noise_cancellation.BVCTelephony(),
    )
    _session_kwargs = dict(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
        room_input_options=_room_input_options,
    )

    t_session_start = time.time()
    await _log("info", f"[LATENCY AUDIT] Connecting to Live API (session.start) at {t_session_start - call_start_time:.2f}s")
    await session.start(**_session_kwargs)
    await _log("info", f"[LATENCY AUDIT] Live API connected / session started in {time.time() - t_session_start:.2f}s")
    await _log("info", "Agent session started — AI ready, generating greeting")

    # ── Optional S3 recording (Asynchronous background task) ─────────────────
    async def start_recording_background():
        _aws_key    = (await get_setting("S3_ACCESS_KEY_ID")) or os.getenv("S3_ACCESS_KEY_ID", "")
        _aws_secret = (await get_setting("S3_SECRET_ACCESS_KEY")) or os.getenv("S3_SECRET_ACCESS_KEY", "")
        _aws_bucket = (await get_setting("S3_BUCKET")) or os.getenv("S3_BUCKET", "")
        _s3_endpoint = (await get_setting("S3_ENDPOINT_URL")) or os.getenv("S3_ENDPOINT_URL", "")
        _s3_region  = (await get_setting("S3_REGION")) or os.getenv("S3_REGION", "ap-northeast-1")
        if _aws_key and _aws_secret and _aws_bucket:
            try:
                _recording_path = f"recordings/{ctx.room.name}.ogg"
                _egress_req = api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name, audio_only=True,
                    file=api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG, filepath=_recording_path,
                        s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret,
                                        bucket=_aws_bucket, region=_s3_region, endpoint=_s3_endpoint,
                                        force_path_style=True),
                    ),
                )
                _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                _s3_ep = _s3_endpoint.rstrip("/")
                _supabase_url = os.getenv("SUPABASE_URL")
                if _supabase_url:
                    _supabase_url = _supabase_url.rstrip("/")
                    tool_ctx.recording_url = f"{_supabase_url}/storage/v1/object/public/{_aws_bucket}/{_recording_path}"
                elif _s3_ep and "supabase.co" in _s3_ep:
                    _base = _s3_ep.split("/storage/v1/s3")[0]
                    tool_ctx.recording_url = f"{_base}/storage/v1/object/public/{_aws_bucket}/{_recording_path}"
                elif _s3_ep:
                    tool_ctx.recording_url = f"{_s3_ep}/{_aws_bucket}/{_recording_path}"
                else:
                    tool_ctx.recording_url = f"https://{_aws_bucket}.s3.amazonaws.com/{_recording_path}"
                await _log("info", f"Recording started: egress={_egress.egress_id}")
            except Exception as _exc:
                await _log("warning", f"Recording start failed (non-fatal): {_exc}")

    if phone_number:
        asyncio.create_task(start_recording_background())

    # ── Greeting ─────────────────────────────────────────────────────────────
    # Give a small 0.5-second delay to ensure SIP audio media bridging is fully complete and active in both directions
    await asyncio.sleep(0.5)
    greeting = (
        f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}."
        if phone_number else "Greet the caller warmly."
    )
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
    if use_realtime:
        try:
            t_greet_start = time.time()
            await _log("info", f"[LATENCY AUDIT] Triggering direct Live API greeting. Session time: {t_greet_start - call_start_time:.2f}s")
            
            # Bypasses the mutable_chat_context blocks in the plugin by sending the Content trigger directly
            turns = [
                _gt.Content(parts=[_gt.Part(text=greeting)], role="user")
            ]
            rt_session = session._activity.realtime_llm_session
            if rt_session is not None:
                rt_session._send_client_event(_gt.LiveClientContent(turns=turns, turn_complete=True))
                await _log("info", f"[LATENCY AUDIT] Direct LiveClientContent greeting trigger sent successfully in {time.time() - t_greet_start:.2f}s")
            else:
                await _log("error", "Direct Live API greeting failed: realtime_llm_session is None")
        except Exception as _inner_exc:
            await _log("error", f"Fallback custom trigger failed: {_inner_exc}")
    else:
        try:
            t_greet_start = time.time()
            await _log("info", f"[LATENCY AUDIT] Triggering pipeline greeting reply. Session time: {t_greet_start - call_start_time:.2f}s")
            await session.generate_reply(instructions=greeting)
            await _log("info", f"[LATENCY AUDIT] Pipeline greeting reply triggered successfully in {time.time() - t_greet_start:.2f}s")
        except Exception as _gr_exc:
            await _log("error", f"Pipeline greeting reply failed: {_gr_exc}")

    # ── Keep session alive until SIP participant actually leaves ─────────────
    # Without this block, the entrypoint returns and the process spins down.
    # We watch participant_disconnected for the specific SIP identity.
    if phone_number:
        _sip_identity = f"sip_{phone_number}"
        _disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == _sip_identity:
                _disconnect_event.set()
        def _on_disconnected():
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")

        await _log("info", f"SIP participant disconnected — ending session for {phone_number}")
        await session.aclose()
        if getattr(tool_ctx, "call_active", True):
            from db import log_call
            duration = int(time.time() - tool_ctx._call_start_time)
            await log_call(
                phone_number, lead_name, "dropped", "Lead hung up before completion",
                duration, getattr(tool_ctx, "recording_url", None)
            )
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    load_db_settings_to_env()   # load DB settings before init_db so Supabase URL is set
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
