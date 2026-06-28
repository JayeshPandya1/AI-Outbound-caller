import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

# ---------------------------------------------------------------------------
# DEFAULTS — all loaded from environment variables only.
# Never hardcode real credentials here. Use Coolify env vars or .env file.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "LIVEKIT_URL":             os.getenv("LIVEKIT_URL", ""),
    "LIVEKIT_API_KEY":         os.getenv("LIVEKIT_API_KEY", ""),
    "LIVEKIT_API_SECRET":      os.getenv("LIVEKIT_API_SECRET", ""),
    "GOOGLE_API_KEY":          os.getenv("GOOGLE_API_KEY", ""),
    "GEMINI_MODEL":            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    "GEMINI_TTS_VOICE":        os.getenv("GEMINI_TTS_VOICE", "Aoede"),
    "USE_GEMINI_REALTIME":     os.getenv("USE_GEMINI_REALTIME", "true"),
    "VOBIZ_SIP_DOMAIN":        os.getenv("VOBIZ_SIP_DOMAIN", ""),
    "VOBIZ_USERNAME":          os.getenv("VOBIZ_USERNAME", ""),
    "VOBIZ_PASSWORD":          os.getenv("VOBIZ_PASSWORD", ""),
    "VOBIZ_OUTBOUND_NUMBER":   os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    "OUTBOUND_TRUNK_ID":       os.getenv("OUTBOUND_TRUNK_ID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL":            os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY":    os.getenv("SUPABASE_SERVICE_KEY", ""),
    "DEEPGRAM_API_KEY":        os.getenv("DEEPGRAM_API_KEY", ""),
    "VOBIZ_AUTH_ID":           os.getenv("VOBIZ_AUTH_ID", ""),
    "VOBIZ_AUTH_TOKEN":        os.getenv("VOBIZ_AUTH_TOKEN", ""),
}


def _default(key: str) -> str:
    """Always read from os.environ so dotenv-loaded values are picked up."""
    return os.getenv(key, DEFAULTS.get(key, ""))


# NOTE: Do NOT cache SUPABASE_URL/KEY at module level — dotenv may not be loaded yet.
# Always call _default() inside functions so env vars are fresh.

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY", "VOBIZ_WEBHOOK_SECRET", "VOBIZ_AUTH_TOKEN",
}


def _sdb():
    from supabase import create_client
    return create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


# ── Cached async Supabase client (singleton) ─────────────────────────────────
# PERF: Avoids creating a new HTTPS/TLS connection on every DB call.
# Before this fix, each _adb() call added ~200-500ms of TLS handshake overhead.
_async_client = None
_async_client_lock = asyncio.Lock()

async def _adb():
    global _async_client
    if _async_client is not None:
        return _async_client
    async with _async_client_lock:
        if _async_client is not None:
            return _async_client
        from supabase._async.client import create_client
        _async_client = await create_client(
            _default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY")
        )
        return _async_client


def sync_dotenv_to_db() -> None:
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return
    try:
        db = _sdb()
        KNOWN_KEYS = [
            "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
            "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
            "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
            "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
            "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
            "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
            "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
            "ENABLED_TOOLS", "VOBIZ_WEBHOOK_SECRET", "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN",
        ]
        # Fetch existing settings from Supabase to avoid overwriting them with env vars
        res = db.table("settings").select("key, value").execute()
        existing = {row["key"]: row["value"] for row in res.data or []}

        rows = []
        updated_at = datetime.now(timezone.utc).isoformat()
        for k in KNOWN_KEYS:
            val = os.getenv(k)
            if val is not None and val != "":
                # If key already exists in DB with a non-empty value, do not overwrite it
                # EXCEPT for sensitive credentials which must sync from the environment
                if k in existing and existing[k] is not None and existing[k] != "":
                    if k not in SENSITIVE_KEYS:
                        continue
                rows.append({"key": k, "value": str(val), "updated_at": updated_at})
        if rows:
            db.table("settings").upsert(rows, on_conflict="key").execute()
            print(f"Synced {len(rows)} settings from .env to database successfully")
    except Exception as exc:
        print(f"WARNING: Syncing .env to database failed: {exc}")


def init_db() -> None:
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("WARNING: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("Supabase connected OK")
        sync_dotenv_to_db()
    except Exception as exc:
        print(f"WARNING: Supabase connection failed: {exc}")
        print("   Run supabase_schema.sql in your Supabase Dashboard -> SQL Editor")


# ── Settings Cache (PERF: Reduces settings retrieval latency to 0ms after first lookup) ──
_settings_cache = {}
_settings_cache_lock = asyncio.Lock()

# ── Contact Cache (PERF: Caches CRM contact queries for 0ms lookup latency) ──
_contact_cache = {}
_contact_cache_lock = asyncio.Lock()


async def get_all_settings() -> dict:
    db = await _adb()
    result = await db.table("settings").select("key, value").execute()
    KNOWN_KEYS = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
        "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
        "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
        "ENABLED_TOOLS", "VOBIZ_WEBHOOK_SECRET", "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN",
    ]
    out: dict = {}
    for k in KNOWN_KEYS:
        env_val = _default(k)
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(env_val)}
        else:
            out[k] = {"value": env_val, "configured": bool(env_val)}
    for row in (result.data or []):
        k, v = row["key"], row["value"]
        if k == "TEST_KEY":
            continue
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(v)}
        else:
            out[k] = {"value": v, "configured": bool(v)}
    return out


async def save_settings(data: dict) -> None:
    db = await _adb()
    updated_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {"key": k, "value": str(v), "updated_at": updated_at}
        for k, v in data.items()
        if v is not None and v != ""
    ]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()
        async with _settings_cache_lock:
            for k, v in data.items():
                if v is not None and v != "":
                    _settings_cache[k] = str(v)


async def get_setting(key: str, default: str = "") -> str:
    global _settings_cache
    if key in _settings_cache:
        return _settings_cache[key]
    
    # Check preloaded environment variables first to avoid Supabase lookup latency
    env_val = os.getenv(key)
    if env_val is not None and env_val != "":
        _settings_cache[key] = env_val
        return env_val
    
    async with _settings_cache_lock:
        if key in _settings_cache:
            return _settings_cache[key]
        
        env_val = os.getenv(key)
        if env_val is not None and env_val != "":
            _settings_cache[key] = env_val
            return env_val
        
        db = await _adb()
        result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        val = ""
        if result and result.data:
            val = result.data["value"]
        else:
            val = _default(key) or default
        
        _settings_cache[key] = val
        return val


async def set_setting(key: str, value: str) -> None:
    db = await _adb()
    await db.table("settings").upsert(
        {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="key",
    ).execute()
    async with _settings_cache_lock:
        _settings_cache[key] = value


async def get_enabled_tools() -> list:
    raw = await get_setting("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Error logs ────────────────────────────────────────────────────────────────

async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()),
            "source": source,
            "level": level,
            "message": message[:500],
            "detail": detail[:2000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass


async def get_errors(limit: int = 100) -> list:
    db = await _adb()
    result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    if source:
        query = query.eq("source", source)
    result = await query.execute()
    return result.data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


# ── Appointments ──────────────────────────────────────────────────────────────

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    await db.table("appointments").insert({
        "id": full_id, "name": name, "phone": phone,
        "date": date, "time": time, "service": service,
        "status": "booked", "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    async with _contact_cache_lock:
        if phone in _contact_cache:
            _contact_cache[phone].pop("appointments", None)
    return booking_id


async def check_slot(date: str, time: str) -> bool:
    """Returns True if slot is available (no existing booking)."""
    db = await _adb()
    result = await (
        db.table("appointments").select("id")
        .eq("date", date).eq("time", time).eq("status", "booked")
        .maybe_single().execute()
    )
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        dt = datetime.now(ist_tz).replace(minute=0, second=0, microsecond=0, tzinfo=None) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    result = await (
        db.table("appointments").update({"status": "cancelled"})
        .eq("id", appointment_id).eq("status", "booked").execute()
    )
    return len(result.data or []) > 0


async def get_appointments_by_phone(phone: str) -> list:
    if phone in _contact_cache and "appointments" in _contact_cache[phone]:
        return _contact_cache[phone]["appointments"]
    db = await _adb()
    result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
    val = result.data or []
    async with _contact_cache_lock:
        if phone not in _contact_cache:
            _contact_cache[phone] = {}
        _contact_cache[phone]["appointments"] = val
    return val


# ── Call logs ─────────────────────────────────────────────────────────────────

async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
) -> str:
    db = await _adb()
    call_id = str(uuid.uuid4())
    row: dict = {
        "id": call_id, "phone_number": phone_number, "lead_name": lead_name,
        "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    await db.table("call_logs").insert(row).execute()
    async with _contact_cache_lock:
        if phone_number in _contact_cache:
            _contact_cache[phone_number].pop("calls", None)
    return call_id


async def update_call_outcome(call_id: str, outcome: str, reason: str, duration_seconds: int) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({
        "outcome": outcome,
        "reason": reason,
        "duration_seconds": duration_seconds
    }).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()
    return result.data or []


async def get_calls_by_phone(phone: str) -> list:
    if phone in _contact_cache and "calls" in _contact_cache[phone]:
        return _contact_cache[phone]["calls"]
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
    val = result.data or []
    async with _contact_cache_lock:
        if phone not in _contact_cache:
            _contact_cache[phone] = {}
        _contact_cache[phone]["calls"] = val
    return val


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def update_call_recording(call_id: str, recording_url: str, duration_seconds: Optional[int] = None) -> bool:
    db = await _adb()
    update_data = {"recording_url": recording_url}
    if duration_seconds is not None:
        update_data["duration_seconds"] = duration_seconds
    result = await db.table("call_logs").update(update_data).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts() -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).execute()
    rows = result.data or []
    contacts: dict = {}
    for row in rows:
        phone = row["phone_number"]
        if phone not in contacts:
            contacts[phone] = {
                "phone_number": phone, "lead_name": row.get("lead_name"),
                "total_calls": 0, "booked": 0,
                "last_call": row["timestamp"], "last_outcome": row.get("outcome"),
            }
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(
    filter_type: Optional[str] = "last_week",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> dict:
    from datetime import time
    db = await _adb()
    
    query = db.table("call_logs").select("outcome, duration_seconds, timestamp")
    
    now = datetime.now(timezone.utc)
    
    if filter_type == "today":
        start_dt = datetime.combine(now.date(), time.min).replace(tzinfo=timezone.utc)
        query = query.gte("timestamp", start_dt.isoformat())
    elif filter_type == "yesterday":
        today_start = datetime.combine(now.date(), time.min).replace(tzinfo=timezone.utc)
        yesterday_start = today_start - timedelta(days=1)
        query = query.gte("timestamp", yesterday_start.isoformat()).lt("timestamp", today_start.isoformat())
    elif filter_type == "last_week":
        start_dt = now - timedelta(days=7)
        query = query.gte("timestamp", start_dt.isoformat())
    elif filter_type == "last_month":
        start_dt = now - timedelta(days=30)
        query = query.gte("timestamp", start_dt.isoformat())
    elif filter_type == "custom" and start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            start_dt = datetime.combine(s_date, time.min).replace(tzinfo=timezone.utc)
            query = query.gte("timestamp", start_dt.isoformat())
            if end_date:
                e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
                end_dt = datetime.combine(e_date, time.max).replace(tzinfo=timezone.utc)
                query = query.lte("timestamp", end_dt.isoformat())
        except ValueError:
            pass

    rows = (await query.execute()).data or []
    
    total_calls    = len(rows)
    booked         = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations      = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    
    total_dur      = sum(durations) if durations else 0
    avg_dur        = sum(durations) / len(durations) if durations else 0
    booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)
    
    # Outcomes breakdown
    outcomes: dict = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
        
    # Dynamic timeline buckets (hourly for today/yesterday, daily for others)
    if filter_type in ("today", "yesterday"):
        hourly = {f"{h:02d}:00": 0 for h in range(24)}
        for r in rows:
            ts = r.get("timestamp")
            if ts:
                hr = ts[11:13]
                if hr.isdigit():
                    lbl = f"{int(hr):02d}:00"
                    hourly[lbl] = hourly.get(lbl, 0) + 1
        timeline = [{"date": k, "count": v} for k, v in sorted(hourly.items())]
    else:
        daily: dict = {}
        for r in rows:
            ts = (r.get("timestamp") or "")[:10]
            if ts:
                daily[ts] = daily.get(ts, 0) + 1
        
        if filter_type == "last_week":
            num_days = 7
            start_day = now.date() - timedelta(days=6)
        elif filter_type == "last_month":
            num_days = 30
            start_day = now.date() - timedelta(days=29)
        elif filter_type == "custom" and start_date and end_date:
            try:
                s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
                e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
                num_days = (e_date - s_date).days + 1
                start_day = s_date
            except Exception:
                num_days = 14
                start_day = now.date() - timedelta(days=13)
        else:
            num_days = 14
            start_day = now.date() - timedelta(days=13)
            
        timeline = []
        for i in range(num_days):
            d = (start_day + timedelta(days=i)).isoformat()
            timeline.append({"date": d, "count": daily.get(d, 0)})

    # Avg duration by outcome
    dur_sum: dict = {}
    dur_cnt: dict = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        sec = r.get("duration_seconds")
        if sec:
            dur_sum[o] = dur_sum.get(o, 0.0) + sec
            dur_cnt[o] = dur_cnt.get(o, 0) + 1
    duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}
    
    return {
        "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1), "total_duration_seconds": total_dur,
        "booking_rate_percent": booking_rate,
        "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
    }


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 3,
    system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None,
) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row: dict = {
        "id": campaign_id, "name": name, "status": "active",
        "contacts_json": contacts_json, "schedule_type": schedule_type,
        "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(), "total_dispatched": 0, "total_failed": 0,
    }
    if system_prompt:
        row["system_prompt"] = system_prompt
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    await db.table("campaigns").insert(row).execute()
    return campaign_id


async def get_all_campaigns() -> list:
    db = await _adb()
    result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return result.data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "total_dispatched": dispatched, "total_failed": failed, "status": "completed",
    }).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


# ── Contact Memory ────────────────────────────────────────────────────────────

async def clear_contact_cache(phone: str) -> None:
    async with _contact_cache_lock:
        _contact_cache.pop(phone, None)


async def add_contact_memory(phone: str, insight: str) -> None:
    db = await _adb()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": insight[:1000], "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    await clear_contact_cache(phone)


async def get_contact_memory(phone: str) -> list:
    if phone in _contact_cache and "memory" in _contact_cache[phone]:
        return _contact_cache[phone]["memory"]
    db = await _adb()
    result = await (
        db.table("contact_memory").select("insight, created_at")
        .eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()
    )
    val = result.data or []
    async with _contact_cache_lock:
        if phone not in _contact_cache:
            _contact_cache[phone] = {}
        _contact_cache[phone]["memory"] = val
    return val


async def compress_contact_memory(phone: str, compressed: str) -> None:
    db = await _adb()
    await db.table("contact_memory").delete().eq("phone_number", phone).execute()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone,
        "insight": compressed[:2000], "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    await clear_contact_cache(phone)


# ── Agent Profiles ────────────────────────────────────────────────────────────

async def get_all_agent_profiles() -> list:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").order("created_at").execute()
    return result.data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def create_agent_profile(
    name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview",
    system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False,
) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({
        "id": profile_id, "name": name, "voice": voice, "model": model,
        "system_prompt": system_prompt, "enabled_tools": enabled_tools,
        "is_default": 1 if is_default else 0, "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()


# ── Simple Authentication Helpers ──────────────────────────────────────────────

import hashlib

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return f"{salt.hex()}:{key.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_hex, key_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        actual_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return actual_key == expected_key
    except Exception:
        return False


async def get_user_by_login_id(login_id: str) -> Optional[dict]:
    db = await _adb()
    res = await db.table("users").select("*").eq("login_id", login_id).execute()
    return res.data[0] if res.data else None


async def update_user(user_id: str, updates: dict) -> None:
    db = await _adb()
    await db.table("users").update(updates).eq("id", user_id).execute()


async def create_user_session(user_id: str, session_token: str, user_agent: Optional[str], ip_address: Optional[str], duration_hours: int = 24) -> dict:
    db = await _adb()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    session_id = str(uuid.uuid4())
    row = {
        "id": session_id,
        "user_id": user_id,
        "session_token": session_token,
        "user_agent": user_agent,
        "ip_address": ip_address,
        "last_active": now_str,
        "expires_at": expires_at,
        "created_at": now_str
    }
    await db.table("user_sessions").insert(row).execute()
    return row


async def get_user_session(session_token: str) -> Optional[dict]:
    db = await _adb()
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        res = await db.table("user_sessions").select("*, users(*)").eq("session_token", session_token).execute()
        if not res.data:
            return None
        session = res.data[0]
        # Check expiry
        if session["expires_at"] < now_str:
            await delete_user_session(session_token)
            return None
        
        # Update last active
        await db.table("user_sessions").update({"last_active": now_str}).eq("session_token", session_token).execute()
        return session
    except Exception as exc:
        print(f"WARNING: get_user_session failed: {exc}")
        return None


async def delete_user_session(session_token: str) -> None:
    db = await _adb()
    await db.table("user_sessions").delete().eq("session_token", session_token).execute()


async def get_all_active_sessions_for_user(user_id: str) -> list:
    db = await _adb()
    try:
        res = await db.table("user_sessions").select("*").eq("user_id", user_id).order("last_active", desc=True).execute()
        return res.data or []
    except Exception:
        return []


async def delete_user_session_by_id(session_id: str) -> None:
    db = await _adb()
    await db.table("user_sessions").delete().eq("id", session_id).execute()


async def ensure_default_user() -> None:
    db = await _adb()
    try:
        res = await db.table("users").select("id").limit(1).execute()
        if not res.data:
            admin_id = os.getenv("ADMIN_LOGIN_ID", "admin")
            admin_pass = os.getenv("ADMIN_PASSWORD", "admin123!")
            hashed_pw = hash_password(admin_pass)
            await db.table("users").insert({
                "id": str(uuid.uuid4()),
                "login_id": admin_id,
                "password_hash": hashed_pw,
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            print(f"Created default admin user: {admin_id}")
    except Exception as exc:
        print(f"WARNING: ensure_default_user failed: {exc}. Run Supabase script.")


# ── Batch Runs ─────────────────────────────────────────────────────────────────

import json as _json_mod

async def create_batch_run(
    name: str, contacts: list, call_delay_seconds: int = 3,
    agent_profile_id: Optional[str] = None,
) -> str:
    """Create a new batch run record in Supabase and return its UUID."""
    db = await _adb()
    batch_id = str(uuid.uuid4())
    contacts_with_status = [
        {
            "phone": c.get("phone", ""),
            "lead_name": c.get("lead_name", ""),
            "business_name": c.get("business_name", ""),
            "service_type": c.get("service_type", ""),
            "status": "pending",
            "outcome": None,
            "error_message": None,
        }
        for c in contacts
    ]
    row: dict = {
        "id": batch_id,
        "name": name,
        "status": "running",
        "contacts_json": _json_mod.dumps(contacts_with_status),
        "total_contacts": len(contacts),
        "completed_count": 0,
        "current_index": 0,
        "call_delay_seconds": call_delay_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    await db.table("batch_runs").insert(row).execute()
    return batch_id


async def get_batch_run(batch_id: str) -> Optional[dict]:
    """Fetch a single batch run by ID."""
    db = await _adb()
    result = await db.table("batch_runs").select("*").eq("id", batch_id).maybe_single().execute()
    return result.data if result else None


async def get_active_batch_run() -> Optional[dict]:
    """Return the currently running batch (if any)."""
    db = await _adb()
    result = await db.table("batch_runs").select("*").eq("status", "running").order("created_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


async def get_all_batch_runs() -> list:
    """Return all batch runs ordered newest-first (for Batch Log tab)."""
    db = await _adb()
    result = await (
        db.table("batch_runs")
        .select("id, name, status, total_contacts, completed_count, current_index, call_delay_seconds, agent_profile_id, created_at, completed_at")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


async def update_batch_contact_status(
    batch_id: str, index: int, status: str,
    outcome: Optional[str], error: Optional[str]
) -> None:
    """Update a single contact's status within the batch and increment the completed counter."""
    db = await _adb()
    result = await db.table("batch_runs").select("contacts_json, completed_count").eq("id", batch_id).maybe_single().execute()
    if not result or not result.data:
        return
    contacts = _json_mod.loads(result.data.get("contacts_json") or "[]")
    if index < len(contacts):
        contacts[index]["status"] = status
        contacts[index]["outcome"] = outcome
        contacts[index]["error_message"] = error
    # Only count terminal statuses toward completed_count
    completed_count = result.data.get("completed_count", 0)
    if status in ("completed", "not_responded", "failed"):
        completed_count += 1
    await db.table("batch_runs").update({
        "contacts_json": _json_mod.dumps(contacts),
        "completed_count": completed_count,
        "current_index": index,
    }).eq("id", batch_id).execute()


async def update_batch_run_status(batch_id: str, status: str) -> None:
    """Set the top-level status of a batch run (completed / stopped / failed)."""
    db = await _adb()
    updates: dict = {"status": status}
    if status in ("completed", "stopped", "failed"):
        updates["completed_at"] = datetime.now(timezone.utc).isoformat()
    await db.table("batch_runs").update(updates).eq("id", batch_id).execute()
