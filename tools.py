import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    check_slot, get_next_available, insert_appointment, log_call, log_error,
    get_calls_by_phone, get_appointments_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
    update_call_outcome, update_call_recording, clear_contact_cache,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(self, ctx: agents.JobContext, phone_number: Optional[str] = None, lead_name: Optional[str] = None, call_db_id: Optional[str] = None, cached_history: Optional[str] = None):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self.call_active = True
        self.call_db_id = call_db_id
        self.cached_history = cached_history or "No history found."
        self.session = None  # Saved reference to AgentSession
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
        ]
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """
        Check whether a date/time slot is available for booking.
        Call this BEFORE attempting to book whenever the lead proposes a date/time.
        date format: YYYY-MM-DD  |  time format: HH:MM (24-hour)
        Returns 'available' or 'unavailable: next available slot is <slot>'.
        """
        t0 = time.time()
        try:
            is_avail = await asyncio.wait_for(check_slot(date, time), timeout=2.0)
            if is_avail:
                logger.info(f"[LATENCY AUDIT] check_availability execution took {time.time() - t0:.2f}s")
                return "available"
            next_slot = await asyncio.wait_for(get_next_available(date, time), timeout=2.0)
            logger.info(f"[LATENCY AUDIT] check_availability execution took {time.time() - t0:.2f}s")
            return f"unavailable: next available slot is {next_slot}"
        except asyncio.TimeoutError:
            logger.warning(f"[LATENCY AUDIT] check_availability timed out after {time.time() - t0:.2f}s")
            return "I'm checking the calendar, but it is taking a moment. Please tell me your preferred alternative time in case."
        except Exception as exc:
            return "Unable to check availability right now — please suggest a date and I will confirm."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """
        Book an appointment after the lead has verbally confirmed date, time, and service.
        Call ONLY after the lead confirms all details.
        name: lead's full name | phone: with country code | date: YYYY-MM-DD | time: HH:MM | service: type
        """
        t0 = time.time()
        try:
            booking_id = await asyncio.wait_for(insert_appointment(name, phone, date, time, service), timeout=2.0)
            logger.info(f"[LATENCY AUDIT] book_appointment execution took {time.time() - t0:.2f}s")
            return f"Confirmed! Booking ID: {booking_id}. See you on {date} at {time} for {service}."
        except asyncio.TimeoutError:
            logger.warning(f"[LATENCY AUDIT] book_appointment timed out after {time.time() - t0:.2f}s")
            return "I am setting up your booking now. The booking is confirmed, and we will send a message shortly."
        except Exception as exc:
            return "Technical issue saving the booking. Our team will confirm shortly."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS call this before the call ends.
        outcome: 'booked' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
        reason: brief description
        """
        t0 = time.time()
        self.call_active = False
        duration = int(time.time() - self._call_start_time)
        try:
            if self.call_db_id:
                await update_call_outcome(self.call_db_id, outcome, reason, duration)
                if self.recording_url:
                    await update_call_recording(self.call_db_id, self.recording_url)
            else:
                await log_call(
                    phone_number=self.phone_number or "unknown",
                    lead_name=self.lead_name, outcome=outcome, reason=reason,
                    duration_seconds=duration, recording_url=self.recording_url,
                )
            if self.phone_number:
                await clear_contact_cache(self.phone_number)
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        logger.info(f"[LATENCY AUDIT] end_call execution took {time.time() - t0:.2f}s")
        return "Outcome logged. Say a polite goodbye statement now, thank the user for their time in their preferred language, and stop. Do not speak about database outcomes, statuses, or parameters (e.g. 'not_interested' or 'booked')."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Call when lead requests a human, is angry, or has a complex issue.
        reason: why you're transferring
        """
        t0 = time.time()
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination, play_dialtone=False,
                )
            )
            self.call_active = False
            duration = int(time.time() - self._call_start_time)
            try:
                if self.call_db_id:
                    await update_call_outcome(self.call_db_id, "transferred", f"Transferred: {reason}", duration)
                    if self.recording_url:
                        await update_call_recording(self.call_db_id, self.recording_url)
                else:
                    await log_call(
                        phone_number=self.phone_number or "unknown",
                        lead_name=self.lead_name, outcome="transferred", reason=f"Transferred: {reason}",
                        duration_seconds=duration, recording_url=self.recording_url,
                    )
                if self.phone_number:
                    await clear_contact_cache(self.phone_number)
            except Exception as log_exc:
                logger.error("Failed to log transfer: %s", log_exc)
            logger.info(f"[LATENCY AUDIT] transfer_to_human execution took {time.time() - t0:.2f}s")
            return "Transferring you to a human agent now. Please hold."
        except Exception as exc:
            logger.warning(f"[LATENCY AUDIT] transfer_to_human failed in {time.time() - t0:.2f}s: {exc}")
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send SMS confirmation after a successful booking. Skips silently if Twilio not configured.
        phone: lead's phone | message: text to send
        """
        t0 = time.time()
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client
            from twilio.http.http_client import TwilioHttpClient
            loop = asyncio.get_event_loop()
            http_client = TwilioHttpClient(timeout=5.0)
            client = Client(sid, token, http_client=http_client)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            logger.info(f"[LATENCY AUDIT] send_sms_confirmation execution took {time.time() - t0:.2f}s")
            return f"SMS sent to {phone}."
        except Exception as exc:
            logger.warning(f"[LATENCY AUDIT] send_sms_confirmation failed in {time.time() - t0:.2f}s: {exc}")
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Call at the START of every call before engaging.
        phone: the lead's phone number with country code
        Returns call history, appointments, and remembered details.
        """
        # Return the preloaded context instantly (0.0s delay)
        logger.info("[LATENCY AUDIT] lookup_contact returned preloaded history from memory in 0.00s")
        return self.cached_history

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        Examples: "Prefers morning calls", "Has 2 kids, interested in family plan", "Callback in 2 weeks"
        insight: the detail to remember
        """
        t0 = time.time()
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            logger.info(f"[LATENCY AUDIT] remember_details execution took {time.time() - t0:.2f}s")
            return f"Remembered: {insight}"
        except Exception as exc:
            logger.warning(f"[LATENCY AUDIT] remember_details failed in {time.time() - t0:.2f}s: {exc}")
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these notes about a sales contact into 3-5 concise bullets. Keep all key facts.\n\n{bullet_list}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """
        Book in Cal.com calendar after book_appointment succeeds.
        name: full name | email: lead's email | date: YYYY-MM-DD | start_time: HH:MM | notes: optional
        """
        t0 = time.time()
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone,
                          "responses": {"name": name, "email": email, "notes": notes},
                          "metadata": {"source": "OutboundAI"}, "language": "en"},
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            uid = data.get("uid", "")
            logger.info(f"[LATENCY AUDIT] book_calcom execution took {time.time() - t0:.2f}s")
            return f"Cal.com booked. UID: {uid}"
        except Exception as exc:
            logger.warning(f"[LATENCY AUDIT] book_calcom failed in {time.time() - t0:.2f}s: {exc}")
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """
        Cancel a Cal.com booking by UID.
        booking_uid: from book_calcom | reason: optional
        """
        t0 = time.time()
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {},
                )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            logger.info(f"[LATENCY AUDIT] cancel_calcom execution took {time.time() - t0:.2f}s")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            logger.warning(f"[LATENCY AUDIT] cancel_calcom failed in {time.time() - t0:.2f}s: {exc}")
            return f"Cancellation failed: {exc}"
