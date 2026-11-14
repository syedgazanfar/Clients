"""Lead capture, WhatsApp qualification conversation, scoring and follow-ups."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database, iso, utcnow
from .phone import normalize_phone
from .providers import Inbound, SendError

log = logging.getLogger("leadflow.service")

SERVICES = {
    "1": "AI chatbots and assistants",
    "2": "AI integration with existing software",
    "3": "Custom AI model development",
    "4": "AI automation and workflow",
    "5": "Not sure yet",
}
TIMELINES = {
    "1": "As soon as possible",
    "2": "Within 1-3 months",
    "3": "Just exploring",
}
TIMELINE_SCORES = {"As soon as possible": 40, "Within 1-3 months": 25, "Just exploring": 5}

STOP_WORDS = {"STOP", "UNSUBSCRIBE", "OPT OUT", "OPTOUT", "CANCEL", "END"}
START_WORDS = {"START", "SUBSCRIBE", "UNSTOP"}
HUMAN_WORDS = {"HUMAN", "AGENT", "PERSON", "CALL ME", "SPEAK TO SOMEONE", "TALK TO SOMEONE"}

# Order in which missing details are collected.
QUESTION_ORDER = ["name", "company", "service", "challenge", "timeline"]
HOT_SCORE = 60


def _menu(options: dict[str, str]) -> str:
    return "\n".join(f"{key}. {label}" for key, label in options.items())


def question_for(field: str) -> str:
    if field == "name":
        return "To start, what is your name?"
    if field == "company":
        return "Which company or project are you with?"
    if field == "service":
        return "Which of these are you most interested in? Reply with a number.\n" + _menu(SERVICES)
    if field == "challenge":
        return "In a sentence or two, what are you hoping AI can do for your business?"
    if field == "timeline":
        return "When are you looking to get started? Reply with a number.\n" + _menu(TIMELINES)
    raise ValueError(field)


def parse_choice(text: str, options: dict[str, str]) -> str:
    """Map '2', '2.', or a label fragment to the option label; else keep the free text."""
    cleaned = text.strip().rstrip(".)").strip()
    if cleaned in options:
        return options[cleaned]
    lowered = cleaned.lower()
    for label in options.values():
        if lowered and (lowered in label.lower() or label.lower() in lowered):
            return label
    return text.strip()[:200]


def score_lead(lead: dict) -> int:
    score = TIMELINE_SCORES.get(lead.get("timeline") or "", 0)
    if lead.get("company"):
        score += 15
    if lead.get("email"):
        score += 10
    if lead.get("service") and lead["service"] != SERVICES["5"]:
        score += 15
    if len(lead.get("challenge") or "") >= 40:
        score += 20
    return min(score, 100)


def first_name(lead: dict) -> str:
    name = (lead.get("name") or "").strip()
    return name.split()[0] if name else ""


class LeadService:
    def __init__(self, settings: Settings, db: Database, provider):
        self.settings = settings
        self.db = db
        self.provider = provider

    # Sending ---------------------------------------------------------------

    def _send(self, lead: dict, text: str, kind: str = "reply") -> bool:
        if lead["opted_out"] and kind != "optout":
            return False
        try:
            provider_id = self.provider.send(
                lead["phone"], text, kind=kind, name=first_name(lead)
            )
        except SendError as exc:
            log.error("send to lead %s failed: %s", lead["id"], exc)
            self.db.add_message(lead["id"], "out", text, status="failed")
            return False
        self.db.add_message(lead["id"], "out", text, provider_id=provider_id)
        self.db.update_lead(lead["id"], last_outbound_at=iso())
        return True

    def _notify_owner(self, text: str) -> None:
        owner = normalize_phone(
            self.settings.owner_whatsapp, self.settings.default_country_code
        )
        if not owner:
            return
        try:
            self.provider.send(owner, text, kind="owner")
        except SendError as exc:
            log.error("owner notification failed: %s", exc)

    def _summary(self, lead: dict) -> str:
        rows = [
            ("Name", lead.get("name")),
            ("Phone", "+" + lead["phone"]),
            ("Email", lead.get("email")),
            ("Company", lead.get("company")),
            ("Interest", lead.get("service")),
            ("Need", lead.get("challenge")),
            ("Timeline", lead.get("timeline")),
            ("Source", lead.get("source")),
            ("Score", lead.get("score")),
        ]
        return "\n".join(f"{label}: {value}" for label, value in rows if value not in (None, ""))

    # Conversation ----------------------------------------------------------

    def _next_field(self, lead: dict) -> str | None:
        for field in QUESTION_ORDER:
            if not lead.get(field):
                return field
        return None

    def _advance(self, lead: dict, intro: str = "", kind: str = "reply") -> dict:
        """Ask the next missing question, or close out the qualification."""
        field = self._next_field(lead)
        if field:
            lead = self.db.update_lead(lead["id"], stage=f"ask_{field}")
            text = f"{intro}\n\n{question_for(field)}" if intro else question_for(field)
            self._send(lead, text, kind=kind)
            return self.db.get_lead(lead["id"])

        lead = self.db.update_lead(lead["id"], stage="done", score=score_lead(lead))
        closing = (
            f"Thanks {first_name(lead)}, that is everything I need. "
            f"One of the {self.settings.business_name} team will be in touch within one "
            f"business day. If you would like to pick a time now, you can book a free "
            f"30-minute call here: {self.settings.booking_url}"
        )
        if intro:
            closing = f"{intro}\n\n{closing}"
        self._send(lead, closing, kind=kind)
        if not lead["owner_notified"]:
            label = "HOT lead" if lead["score"] >= HOT_SCORE else "Qualified lead"
            self._notify_owner(f"{label}\n{self._summary(lead)}")
            lead = self.db.update_lead(lead["id"], owner_notified=1)
        return self.db.get_lead(lead["id"])

    def _welcome_intro(self, lead: dict) -> str:
        greeting = f"Hi {first_name(lead)}," if first_name(lead) else "Hi,"
        return (
            f"{greeting} thanks for getting in touch with {self.settings.business_name}. "
            "A few quick questions so we can point you to the right engineer. "
            "Reply STOP at any time to opt out."
        )

    # Entry points ----------------------------------------------------------

    def capture_lead(
        self,
        phone: str,
        name: str = "",
        email: str = "",
        company: str = "",
        service: str = "",
        message: str = "",
        consent: bool = False,
        source: str = "website",
    ) -> dict:
        """Store a lead from a form or import. Messages only go out with consent."""
        normalized = normalize_phone(phone, self.settings.default_country_code)
        if not normalized:
            raise ValueError("invalid phone number")
        details = {
            "name": name.strip()[:120],
            "email": email.strip()[:200],
            "company": company.strip()[:200],
            "service": parse_choice(service, SERVICES) if service.strip() else "",
            "challenge": message.strip()[:1000],
        }
        details = {key: value for key, value in details.items() if value}

        existing = self.db.get_lead_by_phone(normalized)
        if existing:
            updates = {k: v for k, v in details.items() if not existing.get(k)}
            if consent and not existing["consent"]:
                updates["consent"] = 1
                if existing["status"] == "prospect":
                    updates["status"] = "active"
            lead = self.db.update_lead(existing["id"], **updates) if updates else existing
            already_contacted = bool(existing["last_outbound_at"] or existing["last_inbound_at"])
            if already_contacted or not lead["consent"] or lead["opted_out"]:
                return lead
        else:
            lead = self.db.create_lead(
                normalized,
                source=source,
                consent=1 if consent else 0,
                status="active" if consent else "prospect",
                **details,
            )
            self._notify_owner(f"New {source} lead\n{self._summary(lead)}")
            if not consent:
                return lead

        return self._advance(lead, intro=self._welcome_intro(lead), kind="welcome")

    def handle_inbound(self, inbound: Inbound) -> dict | None:
        phone = normalize_phone("+" + inbound.phone.lstrip("+"))
        text = inbound.text.strip()
        if not phone or not text:
            return None
        if inbound.provider_id and self.db.message_seen(inbound.provider_id):
            return None

        lead = self.db.get_lead_by_phone(phone)
        is_new = lead is None
        if is_new:
            # The person messaged us first, so replying in this conversation is expected.
            lead = self.db.create_lead(phone, source="whatsapp", consent=1)
        self.db.add_message(lead["id"], "in", text, provider_id=inbound.provider_id)
        lead = self.db.update_lead(lead["id"], last_inbound_at=iso(), followups_sent=0)

        keyword = " ".join(text.upper().split()).strip(".! ")

        if keyword in STOP_WORDS:
            lead = self.db.update_lead(lead["id"], opted_out=1, status="opted_out")
            self._send(
                lead,
                "You have been unsubscribed and will not receive further messages. "
                "Reply START if you change your mind.",
                kind="optout",
            )
            return self.db.get_lead(lead["id"])

        if lead["opted_out"]:
            if keyword not in START_WORDS:
                return lead
            lead = self.db.update_lead(lead["id"], opted_out=0, consent=1, status="active")
            return self._advance(lead, intro="Welcome back.")

        if lead["status"] in ("cold", "prospect"):
            lead = self.db.update_lead(lead["id"], status="active", consent=1)

        if lead["status"] == "handoff" or lead["stage"] == "done":
            self._notify_owner(
                f"Message from {lead.get('name') or 'lead'} (+{lead['phone']}):\n{text}"
            )
            return lead

        if keyword in HUMAN_WORDS:
            lead = self.db.update_lead(lead["id"], status="handoff", score=score_lead(lead))
            self._send(lead, "No problem. A member of the team will message you here shortly.")
            self._notify_owner(f"Lead asked for a person\n{self._summary(lead)}")
            return self.db.get_lead(lead["id"])

        if is_new:
            # Keep a substantive opening message as the description of their need.
            if len(text) >= 25:
                lead = self.db.update_lead(lead["id"], challenge=text[:1000])
            self._notify_owner(f"New WhatsApp lead: +{lead['phone']}\n{text}")
            return self._advance(lead, intro=self._welcome_intro(lead))

        stage = lead["stage"]
        if stage.startswith("ask_"):
            field = stage[4:]
            if field == "service":
                value = parse_choice(text, SERVICES)
            elif field == "timeline":
                value = parse_choice(text, TIMELINES)
            else:
                value = text[:1000]
            lead = self.db.update_lead(lead["id"], **{field: value})
        return self._advance(lead)

    # Follow-ups ------------------------------------------------------------

    def in_send_window(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        return (
            local.weekday() < 5
            and self.settings.quiet_start_hour <= local.hour < self.settings.quiet_end_hour
        )

    def _followup_text(self, lead: dict, number: int, last: bool) -> str:
        greeting = f"Hi {first_name(lead)}," if first_name(lead) else "Hi,"
        if last:
            return (
                f"{greeting} last note from {self.settings.business_name}. If AI is still "
                f"on your list, you can book a free 30-minute call here: "
                f"{self.settings.booking_url}\nReply STOP if you would rather not hear from us."
            )
        field = self._next_field(lead)
        question = question_for(field) if field else ""
        return (
            f"{greeting} just checking in from {self.settings.business_name}. "
            f"Happy to pick up where we left off whenever suits.\n\n{question}"
        ).strip()

    def run_followups(self, now: datetime | None = None) -> int:
        """Nudge consenting leads who went quiet mid-conversation. Returns messages sent."""
        now = now or utcnow()
        if not self.in_send_window(now):
            return 0
        delays = self.settings.followup_delays_hours
        sent = 0
        for lead in self.db.followup_candidates():
            number = lead["followups_sent"]
            last_out = datetime.fromisoformat(lead["last_outbound_at"])
            if number >= len(delays):
                # Sequence finished with no reply: stop contacting this lead.
                if now - last_out >= timedelta(hours=24):
                    self.db.update_lead(lead["id"], status="cold")
                continue
            if now - last_out < timedelta(hours=delays[number]):
                continue
            if sent and self.settings.send_gap_seconds:
                time.sleep(self.settings.send_gap_seconds)
            text = self._followup_text(lead, number, last=number == len(delays) - 1)
            if self._send(lead, text, kind="followup"):
                self.db.update_lead(lead["id"], followups_sent=number + 1)
                sent += 1
        return sent
