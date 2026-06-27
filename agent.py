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
import numpy as np
try:
    import audioop
except ImportError:
    audioop = None

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
from livekit.agents import Agent, AgentSession, RoomInputOptions, io as agents_io
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation
from google.genai import types as _gt



class GateFilteredAudioInput(agents_io.AudioInput):
    def __init__(self, source: agents_io.AudioInput, threshold: float = 150.0):
        super().__init__(label="gate_filtered_input", source=source)
        self.threshold = threshold

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await self.source.__anext__()
        try:
            data_bytes = frame.data
            align_size = frame.num_channels * 2
            
            # Safely pass through misaligned or empty audio fragments
            if len(data_bytes) == 0 or len(data_bytes) % align_size != 0:
                return frame

            if audioop is not None:
                rms = audioop.rms(data_bytes, 2)
            else:
                samples = np.frombuffer(data_bytes, dtype=np.int16)
                rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) if len(samples) > 0 else 0.0
            
            if rms < self.threshold:
                samples_per_channel = len(data_bytes) // align_size
                return rtc.AudioFrame(
                    b'\x00' * len(data_bytes),
                    frame.sample_rate,
                    frame.num_channels,
                    samples_per_channel
                )
        except Exception:
            pass  # Silent safety fallback to prevent blocking/logging in the tight audio loop
        return frame


from db import init_db, log_error, get_enabled_tools, get_setting, log_call, update_call_outcome, SENSITIVE_KEYS
from prompts import build_prompt
from tools import AppointmentTools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# Standalone debugging fallback: forcefully uses this trunk ID instead of Supabase if defined
FALLBACK_TRUNK_ID: Optional[str] = None

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


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
            k = row.get("key")
            v = row.get("value")
            if v and k:
                # Sensitive credentials: environment variables take precedence
                if k in SENSITIVE_KEYS:
                    if not os.environ.get(k):
                        os.environ[k] = v
                else:
                    # Only use database settings if Coolify hasn't set them natively
                    if not os.environ.get(k):
                        os.environ[k] = v
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google Realtime plugin ────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None

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
except ImportError:
    logger.warning("livekit-plugins-google not installed")


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str, gemini_model: str, gemini_voice: str) -> AgentSession:
    """
    Build AgentSession strictly using Gemini Multimodal Live API.

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

    RealtimeClass = _google_realtime or _google_beta_realtime

    if RealtimeClass is None:
        raise RuntimeError(
            "Gemini Live RealtimeModel could not be loaded. "
            "Ensure 'livekit-plugins-google' package is installed and has RealtimeModel support."
        )

    logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
    try:
        # Get VAD parameters from env (configured via Coolify or .env)
        vad_sens_str = os.getenv("GEMINI_VAD_SENSITIVITY", "LOW").upper()
        if vad_sens_str == "HIGH":
            sens_enum = _gt.EndSensitivity.END_SENSITIVITY_HIGH
        elif vad_sens_str == "STANDARD":
            sens_enum = _gt.EndSensitivity.END_SENSITIVITY_STANDARD
        else:
            sens_enum = _gt.EndSensitivity.END_SENSITIVITY_LOW

        try:
            silence_ms = int(os.getenv("GEMINI_VAD_SILENCE_MS", "1000"))
        except ValueError:
            silence_ms = 1000

        try:
            padding_ms = int(os.getenv("GEMINI_VAD_PADDING_MS", "300"))
        except ValueError:
            padding_ms = 300

        _realtime_input_cfg = _gt.RealtimeInputConfig(
            automatic_activity_detection=_gt.AutomaticActivityDetection(
                end_of_speech_sensitivity=sens_enum,
                silence_duration_ms=silence_ms,
                prefix_padding_ms=padding_ms,
            ),
        )
        _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
        _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
            trigger_tokens=10000,
            sliding_window=_gt.SlidingWindow(target_tokens=5000),
        )
        logger.info(f"Silence-prevention config applied: sensitivity={vad_sens_str}, silence={silence_ms}ms, padding={padding_ms}ms (transparent resumption, context compression)")
    except Exception as _cfg_err:
        logger.warning("Could not build silence-prevention config: %s", _cfg_err)
        _realtime_input_cfg = None
        _session_resumption_cfg = None
        _ctx_compression_cfg = None

    realtime_kwargs: dict = dict(
        model=gemini_model, voice=gemini_voice, instructions=system_prompt,
        # PERF: Tuning — lower temperature for faster sampling,
        # max_output_tokens as safety net to prevent runaway generation.
        temperature=0.6,
        max_output_tokens=1024,
        thinking_config=_gt.ThinkingConfig(thinking_level="minimal"),
    )
    if _realtime_input_cfg is not None:
        realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
        realtime_kwargs["session_resumption"]         = _session_resumption_cfg
        realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

    return AgentSession(
        llm=RealtimeClass(**realtime_kwargs),
        tools=tools
    )


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
    _callee_answer_time = 0.0
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
    _sip_identity = f"sip_{phone_number}" if phone_number else None

    call_db_id = None
    if phone_number:
        try:
            call_db_id = await log_call(
                phone_number=phone_number,
                lead_name=lead_name,
                outcome="initiated",
                reason="Call job received, preparing to dial",
                duration_seconds=0
            )
            logger.info(f"Initialized call log in Supabase with ID: {call_db_id}")
            
            # Start pre-caching CRM contact details in the background immediately
            from db import get_calls_by_phone, get_appointments_by_phone, get_contact_memory
            asyncio.create_task(get_calls_by_phone(phone_number))
            asyncio.create_task(get_appointments_by_phone(phone_number))
            asyncio.create_task(get_contact_memory(phone_number))
        except Exception as log_exc:
            logger.error("Failed to initialize call log: %s", log_exc)

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                  service_type=service_type, custom_prompt=custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name, call_db_id=call_db_id)

    # PERF FIX #2: Parallelize all DB lookups with asyncio.gather()
    # Before: 3 sequential get_setting() calls = 0.6-1.5s of serial round-trips.
    # After: 1 wall-clock round-trip for all 3.
    async def _noop(): return None
    _model_coro = get_setting("GEMINI_MODEL", "gemini-3.1-flash-live-preview") if not model_override else _noop()
    _voice_coro = get_setting("GEMINI_TTS_VOICE", "Aoede") if not voice_override else _noop()
    _tools_coro = get_enabled_tools() if not tools_override else _noop()
    _gate_coro = get_setting("SIP_GATE_THRESHOLD", "150.0")
    _model_r, _voice_r, _tools_r, _gate_r = await asyncio.gather(
        _model_coro, _voice_coro, _tools_coro, _gate_coro
    )
    gemini_model = model_override or _model_r
    gemini_voice = voice_override or _voice_r
    
    threshold_val = os.getenv("SIP_GATE_THRESHOLD", _gate_r)
    try:
        silence_threshold = float(threshold_val)
    except ValueError:
        silence_threshold = 150.0
    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = _tools_r if isinstance(_tools_r, list) else []
    else:
        enabled_tools = _tools_r if isinstance(_tools_r, list) else []

    # ── Connect ──────────────────────────────────────────────────────────────
    import time
    call_start_time = time.time()
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Room-level track and participant diagnostic listeners ──────────────────
    @ctx.room.on("track_published")
    def _on_room_track_published(publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[TRACK DIAGNOSTIC] Track PUBLISHED by participant={participant.identity} | Track SID={publication.sid} | Source={publication.source} | Kind={publication.kind}")

    @ctx.room.on("track_subscribed")
    def _on_room_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[TRACK DIAGNOSTIC] Track SUBSCRIBED by participant={participant.identity} | Track SID={publication.sid} | Source={publication.source} | Kind={publication.kind}")

    @ctx.room.on("track_subscription_failed")
    def _on_room_track_sub_failed(participant: rtc.RemoteParticipant, track_sid: str, error: Exception):
        logger.error(f"[TRACK DIAGNOSTIC] Track SUBSCRIPTION FAILED for participant={participant.identity} | Track SID={track_sid} | Error={error}")

    @ctx.room.on("participant_connected")
    def _on_room_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[TRACK DIAGNOSTIC] Participant CONNECTED: identity={participant.identity} | Kind={participant.kind} | Attributes={participant.attributes}")

    @ctx.room.on("participant_disconnected")
    def _on_room_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[TRACK DIAGNOSTIC] Participant DISCONNECTED: identity={participant.identity}")

    # ── Build and start AI session in the background ──
    t_session_init = time.time()
    await _log("info", f"[LATENCY AUDIT] Building AI session (model={gemini_model}) at {t_session_init - call_start_time:.2f}s")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(tools=active_tools, system_prompt=system_prompt, gemini_model=gemini_model, gemini_voice=gemini_voice)
    if session.input.audio is not None:
        session.input.audio = GateFilteredAudioInput(session.input.audio, threshold=silence_threshold)
        logger.info(f"RMS noise gate filter successfully injected into session input audio stream (threshold={silence_threshold}) BEFORE session start.")
    await _log("info", f"[LATENCY AUDIT] Session object built in {time.time() - t_session_init:.2f}s")

    _user_speech_stop_time = 0.0
    _user_speech_start_time = 0.0
    _user_is_speaking = False

    @session.on("user_state_changed")
    def _on_user_state_changed(event):
        logger.info(f"[USER STATE] User state changed: {event.old_state} -> {event.new_state}")

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event):
        logger.info(f"[TRANSCRIPT] User transcribed: '{event.transcript}' at {time.time() - call_start_time:.2f}s (is_final={event.is_final})")

    @session.on("overlapping_speech")
    def _on_overlapping_speech(event):
        logger.info(f"[INTERRUPT] Overlapping speech (barge-in): is_interruption={event.is_interruption} | probability={event.probability:.2f} | total_duration={event.total_duration:.2f}s")

    @session.on("agent_false_interruption")
    def _on_agent_false_interruption(event):
        logger.info(f"[INTERRUPT] Agent false interruption: resumed={event.resumed}")

    @session.on("error")
    def _on_session_error(error):
        logger.error(f"Gemini Live session error: {error}")
        try:
            asyncio.create_task(log_error("agent", f"Gemini Live session error: {error}", level="error"))
        except Exception:
            pass

    _first_audio_sent = False
    _callee_answer_time = 0.0

    @session.on("agent_state_changed")
    def _on_agent_state_changed(event):
        nonlocal _first_audio_sent, _user_speech_stop_time
        new_state = event.new_state
        now = time.time()
        logger.info(f"[AGENT STATE] Agent state changed: {event.old_state} -> {event.new_state}")
        if new_state == "speaking":
            if not _first_audio_sent and _callee_answer_time > 0.0:
                answer_to_audio = now - _callee_answer_time
                logger.info(f"[LATENCY AUDIT] Answer to first agent audio sent: {answer_to_audio * 1000:.0f}ms")
                _first_audio_sent = True
            if _user_speech_stop_time > 0.0:
                vad_to_audio = now - _user_speech_stop_time
                logger.info(f"[LATENCY AUDIT] User speech stopped to first agent audio: {vad_to_audio * 1000:.0f}ms")
                _user_speech_stop_time = 0.0

    from livekit.agents import room_io as _room_io
    _room_options = _room_io.RoomOptions(
        close_on_disconnect=False,
        audio_input=_room_io.AudioInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
        participant_identity=_sip_identity,
    )
    _session_kwargs = dict(
        room=ctx.room,
        agent=OutboundAssistant(instructions=system_prompt),
        room_options=_room_options,
    )

    t_session_start = time.time()
    await _log("info", f"[LATENCY AUDIT] Connecting to Live API (session.start) in background at {t_session_start - call_start_time:.2f}s")
    session_start_task = asyncio.create_task(session.start(**_session_kwargs))

    # ── Dial ──
    if phone_number:
        trunk_id = FALLBACK_TRUNK_ID or await get_setting("OUTBOUND_TRUNK_ID")
        logger.info(f"[DEBUG LOG] Pre-dial check: using trunk_id='{trunk_id}' (FALLBACK_TRUNK_ID='{FALLBACK_TRUNK_ID}')")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            t_dial_start = time.time()
            await _log("info", f"[LATENCY AUDIT] Outbound call initiated to {phone_number} at {t_dial_start - call_start_time:.2f}s")
            
            _sip_identity = f"sip_{phone_number}"
            _answered_event = asyncio.Event()
            _failed_event = asyncio.Event()
            _fail_reason = "Call terminated before answer"

            def _on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
                if participant.identity == _sip_identity:
                    status = participant.attributes.get("sip.callStatus")
                    logger.info(f"Track subscribed for {participant.identity}. Status: {status}, Attributes: {participant.attributes}")
                    if status == "active":
                        _answered_event.set()

            def _on_attributes_changed(changed: dict, participant: rtc.Participant):
                if participant.identity == _sip_identity:
                    status = participant.attributes.get("sip.callStatus")
                    logger.info(f"Attributes changed for {participant.identity}. Status: {status}, Attributes: {participant.attributes}")
                    if status == "active":
                        _answered_event.set()
                    elif status == "hangup":
                        nonlocal _fail_reason
                        _fail_reason = "SIP call refused/rejected (hangup status)"
                        _failed_event.set()

            def _on_participant_disconnected(participant: rtc.RemoteParticipant):
                if participant.identity == _sip_identity:
                    nonlocal _fail_reason
                    _fail_reason = "SIP participant disconnected before answering"
                    _failed_event.set()

            def _on_room_disconnected():
                nonlocal _fail_reason
                _fail_reason = "LiveKit room disconnected before answering"
                _failed_event.set()

            ctx.room.on("track_subscribed", _on_track_subscribed)
            ctx.room.on("participant_attributes_changed", _on_attributes_changed)
            ctx.room.on("participant_disconnected", _on_participant_disconnected)
            ctx.room.on("disconnected", _on_room_disconnected)
            
            outbound_number = await get_setting("VOBIZ_OUTBOUND_NUMBER")
            from datetime import timedelta
            logger.info(f"ATTEMPTING TO DIAL WITH TRUNK ID: '{trunk_id}'")
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    sip_number=outbound_number,
                    participant_identity=_sip_identity,
                    ringing_timeout=timedelta(seconds=30),
                    wait_until_answered=False,
                )
            )
            
            # Check if they connected and are already active/have published a track
            for p in ctx.room.remote_participants.values():
                if p.identity == _sip_identity:
                    status = p.attributes.get("sip.callStatus")
                    if status == "active":
                        _answered_event.set()

            try:
                # Event-driven pickup wait
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(_answered_event.wait()),
                        asyncio.create_task(_failed_event.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=60.0
                )
                for p in pending:
                    p.cancel()
                    
                if not done:
                    raise TimeoutError("Timeout waiting for callee to answer")
                if _failed_event.is_set():
                    raise RuntimeError(_fail_reason)
            except asyncio.TimeoutError:
                raise TimeoutError("Timeout waiting for callee to answer")
            finally:
                ctx.room.off("track_subscribed", _on_track_subscribed)
                ctx.room.off("participant_attributes_changed", _on_attributes_changed)
                ctx.room.off("participant_disconnected", _on_participant_disconnected)
                ctx.room.off("disconnected", _on_room_disconnected)

            await _log("info", f"[LATENCY AUDIT] Callee answered. Ringing/pickup duration: {time.time() - t_dial_start:.2f}s")
        except (Exception, BaseException) as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            try:
                reason_str = f"SIP dial failed: {str(exc)}" if str(exc) else f"SIP dial failed: {type(exc).__name__}"
                if call_db_id:
                    await update_call_outcome(
                        call_id=call_db_id,
                        outcome="no_answer",
                        reason=reason_str,
                        duration_seconds=0
                    )
                else:
                    await log_call(
                        phone_number=phone_number,
                        lead_name=lead_name,
                        outcome="no_answer",
                        reason=reason_str,
                        duration_seconds=0
                    )
            except Exception as log_exc:
                logger.error("Failed to log failed SIP dial: %s", log_exc)
            ctx.shutdown()
            if isinstance(exc, BaseException) and not isinstance(exc, Exception):
                raise exc
            return
        _callee_answer_time = time.time()
        await _log("info", f"Call ANSWERED — {phone_number} picked up, waiting for AI session to be ready")
        tool_ctx._call_start_time = _callee_answer_time

    # Wait for the AI session to finish connecting in the background (if not already done)
    t_session_await = time.time()
    await session_start_task

    t_session_ready = time.time()
    await _log("info", f"[LATENCY AUDIT] Live API connected / session ready (awaited in background for {t_session_ready - t_session_await:.2f}s)")

    # ── Track & Participant subscription checks ──
    try:
        linked = session.room_io.linked_participant
        await _log("info", f"[TRACK DIAGNOSTIC] RoomIO is linked to participant: {linked.identity if linked else 'None'} | Kind={linked.kind if linked else 'N/A'}")
    except Exception as io_exc:
        await _log("warning", f"[TRACK DIAGNOSTIC] RoomIO linked participant check failed: {io_exc}")

    # ── Bind underlying RealtimeSession events ──
    rt_sess = getattr(session._activity, "_rt_session", None)
    if rt_sess is not None:
        @rt_sess.on("input_speech_started")
        def _on_rt_input_speech_started(event):
            nonlocal _user_is_speaking, _user_speech_start_time
            _user_is_speaking = True
            _user_speech_start_time = time.time()
            logger.info("[VAD DETECT] Server VAD: User started speaking")

        @rt_sess.on("input_speech_stopped")
        def _on_rt_input_speech_stopped(event):
            nonlocal _user_speech_stop_time, _user_is_speaking
            _user_speech_stop_time = time.time()
            _user_is_speaking = False
            logger.info(f"[VAD DETECT] Server VAD: User stopped speaking at {_user_speech_stop_time - call_start_time:.2f}s")

        @rt_sess.on("generation_created")
        def _on_rt_generation_created(event):
            nonlocal _user_speech_stop_time, _user_speech_start_time, _user_is_speaking
            now = time.time()
            if _user_is_speaking and _user_speech_start_time > 0.0:
                # User is still speaking; this is an early response / tool call / interruption
                early_latency = now - _user_speech_start_time
                logger.info(f"[AUDIO GEN] Agent response generation started early (user still speaking) (id={event.response_id}) at {now - call_start_time:.2f}s | Reply Latency (VAD start to response): {early_latency * 1000:.0f}ms")
            elif _user_speech_stop_time > 0.0:
                reply_latency = now - _user_speech_stop_time
                logger.info(f"[AUDIO GEN] Agent response generation started (id={event.response_id}) at {now - call_start_time:.2f}s | Reply Latency (VAD to response): {reply_latency * 1000:.0f}ms")
            else:
                logger.info(f"[AUDIO GEN] Agent response generation started (id={event.response_id}) at {now - call_start_time:.2f}s")

        @rt_sess.on("input_audio_transcription_completed")
        def _on_rt_input_audio_transcription_completed(event):
            logger.info(f"[TRANSCRIPT] Underlying User utterance transcription finished: '{event.transcript}' at {time.time() - call_start_time:.2f}s (is_final={event.is_final})")
    else:
        await _log("warning", "[TRACK DIAGNOSTIC] Underlying RealtimeSession is None after session start")

    await _log("info", "Agent session started — AI ready, generating greeting")

    # Wait 1.2s for media cut-through before speaking
    await asyncio.sleep(1.2)

    # Trigger greeting
    try:
        # Check if the model has mutable chat context (Gemini 3.1 Live has mutable_chat_context=False)
        is_mutable = getattr(session.llm, "capabilities", None) is None or getattr(session.llm.capabilities, "mutable_chat_context", True)
        if not is_mutable:
            # For Gemini 3.1 Live API, we push the text event directly to the realtime WebSocket channel.
            # IMPORTANT: Use LiveClientRealtimeInput (NOT LiveClientContent) — using LiveClientContent
            # bypasses the plugin's _pending_generation_fut assignment, causing _handle_input_speech_started()
            # to fire as an unintended interruption after the greeting, which permanently breaks the
            # conversation loop for the rest of the call (agent goes silent after greeting).
            rt_sess = getattr(session._activity, "_rt_session", None)
            if rt_sess is not None:
                event = _gt.LiveClientRealtimeInput(text="[SYSTEM: CALL_CONNECTED]")
                rt_sess._send_client_event(event)
                await _log("info", "[LATENCY AUDIT] Gemini 3.1 greeting triggered successfully via direct LiveClientRealtimeInput.")
            else:
                await _log("warning", "Could not trigger Gemini 3.1 greeting: rt_session is None")
        else:
            # Fallback for models with mutable chat context (like Gemini 2.5) or pipeline fallback
            await session.generate_reply(user_input="[SYSTEM: CALL_CONNECTED]")
            await _log("info", "[LATENCY AUDIT] Greeting reply triggered successfully via generate_reply.")
    except Exception as _gr_exc:
        await _log("error", f"Greeting reply failed: {_gr_exc}")

    # PERF FIX #6: Structured latency summary — all milestones in one log entry
    await _log("info", (
        f"[LATENCY SUMMARY] "
        f"settings={t_session_init - call_start_time:.2f}s | "
        f"session_build={t_session_start - t_session_init:.2f}s | "
        f"session_connect={t_session_ready - t_session_start:.2f}s | "
        f"total_to_ready={t_session_ready - call_start_time:.2f}s"
    ))

    # ── Optional S3 recording (Asynchronous background task) ─────────────────
    async def start_recording_background():
        # PERF FIX #5: Parallelize all 5 S3 settings lookups
        _s3_results = await asyncio.gather(
            get_setting("S3_ACCESS_KEY_ID"), get_setting("S3_SECRET_ACCESS_KEY"),
            get_setting("S3_BUCKET"), get_setting("S3_ENDPOINT_URL"), get_setting("S3_REGION"),
        )
        _aws_key    = _s3_results[0] or os.getenv("S3_ACCESS_KEY_ID", "")
        _aws_secret = _s3_results[1] or os.getenv("S3_SECRET_ACCESS_KEY", "")
        _aws_bucket = _s3_results[2] or os.getenv("S3_BUCKET", "")
        _s3_endpoint = _s3_results[3] or os.getenv("S3_ENDPOINT_URL", "")
        _s3_region  = _s3_results[4] or os.getenv("S3_REGION", "ap-northeast-1")
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
            duration = int(time.time() - tool_ctx._call_start_time)
            if call_db_id:
                try:
                    from db import update_call_recording
                    await update_call_outcome(call_db_id, "dropped", "Lead hung up before completion", duration)
                    rec_url = getattr(tool_ctx, "recording_url", None)
                    if rec_url:
                        await update_call_recording(call_db_id, rec_url)
                except Exception as log_exc:
                    logger.error("Failed to update call log on drop: %s", log_exc)
            else:
                try:
                    await log_call(
                        phone_number=phone_number,
                        lead_name=lead_name,
                        outcome="dropped",
                        reason="Lead hung up before completion",
                        duration_seconds=duration,
                        recording_url=getattr(tool_ctx, "recording_url", None)
                    )
                except Exception as log_exc:
                    logger.error("Failed to log call drop: %s", log_exc)
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
