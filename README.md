# HeyStartup Leadflow

Captures leads from heystartup.co.uk, qualifies them automatically over WhatsApp, follows up
when they go quiet, and sends the good ones to your own WhatsApp.

## What it does

1. **Website widget.** One script tag adds a "Talk to us on WhatsApp" button and form to
   your site. A visitor leaves their name and number and ticks the consent box.
2. **Instant WhatsApp reply.** Within seconds they get a WhatsApp message from you that
   starts a short qualification chat: company, which service, what they need, how soon.
   Anything they already gave in the form is not asked again.
3. **Inbound WhatsApp.** Anyone who messages your number directly goes through the same chat.
4. **Scoring and alerts.** Each lead is scored 0-100. You get a WhatsApp alert for every new
   lead and a full summary (marked HOT at 60 or above) when one finishes qualifying.
5. **Follow-ups.** Leads who stop replying mid-chat get a nudge after 24 hours and a final
   one after a further 72 hours, on weekdays between 09:00 and 19:00 UK time. After that
   they are marked cold and left alone.
6. **Handoff.** Once a lead is qualified, or types "human", the bot goes quiet and their
   messages are forwarded to you.
7. **Dashboard.** `/admin` lists every lead with score, answers and the full conversation,
   plus a CSV export.

## Who gets messaged

Automated messages only go to people who ticked the consent box on the form or who messaged
you first. Replying STOP ends all messages immediately; START re-subscribes. Imported
contacts without recorded consent are stored as prospects and never messaged automatically.

This is deliberate. Bulk messaging people who did not ask to hear from you gets a WhatsApp
number banned quickly, and in the UK it also falls foul of PECR and UK GDPR.

## Quick start (dry run, no WhatsApp needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # set ADMIN_TOKEN at least
.venv/bin/uvicorn leadflow.app:create_app --factory --port 8000
```

With `WHATSAPP_PROVIDER=console` every outgoing message is printed to the log instead of
being sent, so you can try the whole flow safely:

```bash
curl -X POST localhost:8000/api/leads -H 'content-type: application/json' \
  -d '{"name":"Test Lead","phone":"+447700900123","consent":true}'
```

Open http://localhost:8000/admin (any username, password = `ADMIN_TOKEN`).

Run the tests with `.venv/bin/python -m pytest`.

## Connecting WhatsApp

Pick one provider in `.env`.

### Option A: your WA-AKG gateway (wa.invvite.heystartup.co.uk)

This is the gateway you already host ([WA-AKG](https://github.com/mrifqidaffaaditya/WA-AKG)).

1. In the gateway dashboard, make sure the WhatsApp number is connected, then note its
   session ID and copy your API key from the settings page.
2. In `.env` set:

   ```
   WHATSAPP_PROVIDER=akg
   AKG_URL=https://wa.invvite.heystartup.co.uk
   AKG_API_KEY=...            # from the gateway dashboard
   AKG_SESSION=...            # the session ID
   AKG_WEBHOOK_SECRET=...     # any long random string
   ```

   plus `OWNER_WHATSAPP` and `ADMIN_TOKEN`.
3. Deploy Leadflow on a public HTTPS address (the gateway refuses plain-HTTP webhook URLs),
   for example `https://leads.heystartup.co.uk`. With Docker:
   `docker compose up -d --build leadflow`.
4. Tell the gateway where to send incoming messages:

   ```bash
   .venv/bin/python -m leadflow.cli akg-webhook https://leads.heystartup.co.uk/webhooks/akg
   ```

   Or add it by hand in the gateway dashboard under Webhooks: that URL, the same secret,
   event `message.received`.

Two things to check on the gateway side:

- Turn off any gateway auto-reply rules for this session, otherwise leads get two replies.
- If `OWNER_WHATSAPP` is the same number the gateway is connected to, alerts would be sent
  from the number to itself. Use a different number for alerts.

Note: gateways of this kind drive a normal WhatsApp account through WhatsApp Web. That is
not an officially supported route, so keep volume modest.

### Option B: WAHA gateway

[WAHA](https://github.com/devlikeapro/waha) is an open-source WhatsApp HTTP API you host
yourself. `docker-compose.yml` runs it next to Leadflow.

1. In `.env` set `WHATSAPP_PROVIDER=waha`, `WAHA_API_KEY`, `WEBHOOK_TOKEN` (both long
   random strings), `OWNER_WHATSAPP` and `ADMIN_TOKEN`.
2. `docker compose up -d --build`
3. Open the WAHA dashboard on port 3000 of the server, start the `default` session and scan
   the QR code with the WhatsApp account that should send the messages.

If you already host a WAHA gateway, remove the `waha` service from the compose file, set
`WAHA_URL` to it, and configure its message webhook to
`https://YOUR-LEADFLOW-HOST/webhooks/waha?token=YOUR_WEBHOOK_TOKEN`.

The same caution about unofficial gateways applies.

### Option C: official WhatsApp Cloud API

1. In `.env` set `WHATSAPP_PROVIDER=cloud`, `WA_CLOUD_TOKEN`, `WA_CLOUD_PHONE_ID`,
   `WA_CLOUD_VERIFY_TOKEN` and `WA_CLOUD_APP_SECRET`.
2. In the Meta app dashboard set the webhook URL to `https://YOUR-LEADFLOW-HOST/webhooks/cloud`,
   use the same verify token, and subscribe to `messages`.
3. Meta only allows free-form text within 24 hours of the customer's last message. The
   first message to a form lead and the follow-ups therefore need approved templates: create
   two templates with one body variable (the first name) and put their names in
   `WA_CLOUD_WELCOME_TEMPLATE` and `WA_CLOUD_FOLLOWUP_TEMPLATE`.

## Adding the widget to heystartup.co.uk

Host Leadflow on a subdomain with HTTPS (for example `leads.heystartup.co.uk`) and add this
before `</body>` on the site:

```html
<script src="https://leads.heystartup.co.uk/widget.js" data-whatsapp="YOUR_NUMBER" defer></script>
```

`data-whatsapp` (digits with country code) adds a direct "message us on WhatsApp" link.
`data-color` and `data-label` change the accent colour and button text. The API only accepts
form posts from the origins listed in `ALLOWED_ORIGINS`.

To use your own form instead, POST JSON to `/api/leads` with `name`, `phone`, `consent` and
optionally `email`, `company`, `service`, `message`.

## Importing existing contacts

```bash
.venv/bin/python -m leadflow.cli import contacts.csv
```

Columns: `name, phone, email, company, service, message, consent`. Rows with `consent=yes`
receive the welcome message; the rest are stored as prospects for you to contact personally
from the dashboard (each number links to a WhatsApp chat).

## Changing the conversation

Questions, service list, score weights and keywords are plain Python at the top of
`leadflow/service.py`. Follow-up timing and the send window are in `.env`.

## Layout

```
leadflow/app.py        web app: form API, webhooks, dashboard
leadflow/service.py    conversation, scoring, follow-ups
leadflow/providers.py  WA-AKG, WAHA, Cloud API and dry-run senders
leadflow/db.py         SQLite storage
leadflow/cli.py        CSV import, follow-up run, gateway webhook setup
static/widget.js       website widget
tests/                 test suite
```
