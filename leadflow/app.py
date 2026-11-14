"""FastAPI app: lead capture API, WhatsApp webhooks, admin dashboard."""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .config import Settings, load_dotenv
from .db import Database
from .providers import CloudProvider, WahaProvider, build_provider
from .service import HOT_SCORE, LeadService

log = logging.getLogger("leadflow.app")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
RATE_LIMIT = 5  # form submissions per IP per hour


class LeadForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    email: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    service: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=1000)
    consent: bool = False
    website: str = Field(default="", max_length=200)  # honeypot, must stay empty


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a.encode(), b.encode())


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)
    db = Database(settings.db_path)
    service = LeadService(settings, db, provider or build_provider(settings))
    stop = threading.Event()
    hits: dict[str, list[float]] = {}

    def followup_loop() -> None:
        while not stop.wait(settings.followup_interval_seconds):
            try:
                service.run_followups()
            except Exception:
                log.exception("follow-up run failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        thread = None
        if settings.followup_interval_seconds > 0:
            thread = threading.Thread(target=followup_loop, daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=5)

    app = FastAPI(title="HeyStartup Leadflow", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    basic = HTTPBasic(auto_error=False)

    def admin(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.admin_token:
            raise HTTPException(503, "Admin is disabled: set ADMIN_TOKEN")
        if not credentials or not _same(credentials.password, settings.admin_token):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})

    # Public ----------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "provider": service.provider.name}

    @app.get("/widget.js")
    def widget() -> FileResponse:
        return FileResponse(STATIC_DIR / "widget.js", media_type="application/javascript")

    @app.post("/api/leads")
    def create_lead(form: LeadForm, request: Request) -> dict:
        if form.website:
            return {"ok": True}  # bot filled the honeypot: accept silently, store nothing
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in hits.get(ip, []) if now - t < 3600]
        if len(recent) >= RATE_LIMIT:
            raise HTTPException(429, "Too many submissions, please try again later")
        hits[ip] = [*recent, now]
        try:
            service.capture_lead(
                phone=form.phone,
                name=form.name,
                email=form.email,
                company=form.company,
                service=form.service,
                message=form.message,
                consent=form.consent,
            )
        except ValueError:
            raise HTTPException(422, "Please enter a valid phone number with country code")
        return {"ok": True}

    # Webhooks --------------------------------------------------------------

    def process(messages: list) -> int:
        count = 0
        for inbound in messages:
            try:
                service.handle_inbound(inbound)
                count += 1
            except Exception:
                log.exception("failed to handle inbound message")
        return count

    @app.post("/webhooks/waha")
    def waha_webhook(payload: dict, request: Request) -> dict:
        if not settings.webhook_token:
            raise HTTPException(503, "Webhook is disabled: set WEBHOOK_TOKEN")
        supplied = request.headers.get("x-webhook-token") or request.query_params.get("token", "")
        if not _same(supplied, settings.webhook_token):
            raise HTTPException(401, "Unauthorized")
        return {"ok": True, "handled": process(WahaProvider.parse(payload))}

    @app.get("/webhooks/cloud")
    def cloud_verify(request: Request) -> PlainTextResponse:
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and _same(
            params.get("hub.verify_token", ""), settings.cloud_verify_token
        ):
            return PlainTextResponse(params.get("hub.challenge", ""))
        raise HTTPException(403, "Verification failed")

    @app.post("/webhooks/cloud")
    async def cloud_webhook(request: Request) -> dict:
        if not settings.cloud_app_secret:
            raise HTTPException(503, "Webhook is disabled: set WA_CLOUD_APP_SECRET")
        raw = await request.body()
        expected = "sha256=" + hmac.new(
            settings.cloud_app_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        if not _same(request.headers.get("x-hub-signature-256", ""), expected):
            raise HTTPException(401, "Bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "Invalid JSON")
        return {"ok": True, "handled": await run_in_threadpool(process, CloudProvider.parse(payload))}

    # Admin -----------------------------------------------------------------

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def dashboard() -> str:
        return render_dashboard(db.list_leads(), settings)

    @app.get("/admin/leads/{lead_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def lead_detail(lead_id: int) -> str:
        lead = db.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        return render_lead(lead, db.list_messages(lead_id))

    @app.get("/admin/leads.csv", dependencies=[Depends(admin)])
    def export_csv() -> Response:
        leads = db.list_leads()
        buffer = io.StringIO()
        columns = [
            "id", "created_at", "name", "phone", "email", "company", "service",
            "challenge", "timeline", "source", "score", "stage", "status", "consent",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            # Neutralise spreadsheet formula injection from user-supplied text.
            writer.writerow(
                {
                    k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v)
                    for k, v in lead.items()
                }
            )
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leads.csv"},
        )

    @app.post("/admin/followups/run", dependencies=[Depends(admin)])
    def run_followups() -> dict:
        return {"sent": service.run_followups()}

    return app


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
body{{font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1b1f24}}
h1{{font-size:20px;margin:0 0 4px}} a{{color:#0b5fff;text-decoration:none}}
.meta{{color:#5b6570;margin-bottom:16px}}
.wrap{{overflow-x:auto;background:#fff;border:1px solid #e3e6ea;border-radius:8px}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:8px 12px;text-align:left;
border-bottom:1px solid #eef0f3;vertical-align:top;white-space:nowrap}}
td.wide{{white-space:normal;min-width:220px}} th{{font-size:12px;color:#5b6570;background:#fafbfc}}
.tag{{display:inline-block;padding:1px 8px;border-radius:10px;background:#e8ecf1;font-size:12px}}
.hot{{background:#ffe2d6;color:#a33a00}}
.msg{{max-width:560px;margin:8px 0;padding:8px 12px;border-radius:8px;white-space:pre-wrap;
background:#fff;border:1px solid #e3e6ea}} .out{{margin-left:auto;background:#e4f5e1}}
.failed{{border-color:#d33}} .when{{font-size:11px;color:#5b6570}}
</style></head><body>{body}</body></html>"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def render_dashboard(leads: list[dict], settings: Settings) -> str:
    rows = []
    for lead in leads:
        hot = lead["score"] >= HOT_SCORE
        rows.append(
            "<tr>"
            f"<td><span class='tag {'hot' if hot else ''}'>{lead['score']}</span></td>"
            f"<td><a href='/admin/leads/{lead['id']}'>{_e(lead['name'] or 'Unknown')}</a></td>"
            f"<td><a href='https://wa.me/{_e(lead['phone'])}' target='_blank' rel='noopener'>"
            f"+{_e(lead['phone'])}</a></td>"
            f"<td>{_e(lead['company'])}</td><td>{_e(lead['service'])}</td>"
            f"<td class='wide'>{_e(lead['challenge'])}</td><td>{_e(lead['timeline'])}</td>"
            f"<td>{_e(lead['source'])}</td>"
            f"<td><span class='tag'>{_e(lead['status'])}</span> {_e(lead['stage'])}</td>"
            f"<td>{_e(lead['created_at'][:10])}</td></tr>"
        )
    hot_count = sum(1 for lead in leads if lead["score"] >= HOT_SCORE)
    body = (
        f"<h1>{_e(settings.business_name)} leads</h1>"
        f"<div class='meta'>{len(leads)} leads, {hot_count} hot. "
        "<a href='/admin/leads.csv'>Download CSV</a></div>"
        "<div class='wrap'><table><tr><th>Score</th><th>Name</th><th>WhatsApp</th>"
        "<th>Company</th><th>Interest</th><th>Need</th><th>Timeline</th><th>Source</th>"
        "<th>Status</th><th>Created</th></tr>"
        + ("".join(rows) or "<tr><td colspan='10'>No leads yet.</td></tr>")
        + "</table></div>"
    )
    return PAGE.format(title="Leads", body=body)


def render_lead(lead: dict, messages: list[dict]) -> str:
    bubbles = "".join(
        f"<div class='msg {'out' if m['direction'] == 'out' else ''} "
        f"{'failed' if m['status'] != 'ok' else ''}'>{_e(m['body'])}"
        f"<div class='when'>{_e(m['created_at'])}"
        f"{' - not delivered' if m['status'] != 'ok' else ''}</div></div>"
        for m in messages
    )
    details = " | ".join(
        _e(part)
        for part in [
            "+" + lead["phone"], lead["email"], lead["company"], lead["service"],
            lead["timeline"], f"score {lead['score']}", lead["status"],
        ]
        if part
    )
    body = (
        f"<a href='/admin'>Back to leads</a><h1>{_e(lead['name'] or 'Unknown')}</h1>"
        f"<div class='meta'>{details}</div>{bubbles or '<p>No messages yet.</p>'}"
    )
    return PAGE.format(title="Lead", body=body)
