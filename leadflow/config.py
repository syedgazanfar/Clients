"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    business_name: str = "HeyStartup"
    booking_url: str = "https://heystartup.co.uk/#contact"
    db_path: str = "leads.db"
    admin_token: str = ""
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "https://heystartup.co.uk",
            "https://www.heystartup.co.uk",
        ]
    )
    default_country_code: str = "44"
    owner_whatsapp: str = ""

    # WhatsApp provider: console | akg | waha | cloud
    whatsapp_provider: str = "console"
    webhook_token: str = ""

    waha_url: str = "http://localhost:3000"
    waha_api_key: str = ""
    waha_session: str = "default"

    akg_url: str = "https://wa.invvite.heystartup.co.uk"
    akg_api_key: str = ""
    akg_session: str = ""
    akg_webhook_secret: str = ""

    cloud_token: str = ""
    cloud_phone_id: str = ""
    cloud_verify_token: str = ""
    cloud_app_secret: str = ""
    cloud_api_version: str = "v21.0"
    cloud_welcome_template: str = ""
    cloud_followup_template: str = ""
    cloud_template_lang: str = "en_GB"

    # Follow-ups
    followup_delays_hours: list[float] = field(default_factory=lambda: [24.0, 72.0])
    followup_interval_seconds: int = 300
    send_gap_seconds: float = 5.0
    timezone: str = "Europe/London"
    quiet_start_hour: int = 9
    quiet_end_hour: int = 19

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        base = cls()
        return cls(
            business_name=env("BUSINESS_NAME", base.business_name),
            booking_url=env("BOOKING_URL", base.booking_url),
            db_path=env("DB_PATH", base.db_path),
            admin_token=env("ADMIN_TOKEN", ""),
            allowed_origins=_list(env("ALLOWED_ORIGINS", ",".join(base.allowed_origins))),
            default_country_code=env("DEFAULT_COUNTRY_CODE", base.default_country_code),
            owner_whatsapp=env("OWNER_WHATSAPP", ""),
            whatsapp_provider=env("WHATSAPP_PROVIDER", base.whatsapp_provider).lower(),
            webhook_token=env("WEBHOOK_TOKEN", ""),
            waha_url=env("WAHA_URL", base.waha_url).rstrip("/"),
            waha_api_key=env("WAHA_API_KEY", ""),
            waha_session=env("WAHA_SESSION", base.waha_session),
            akg_url=env("AKG_URL", base.akg_url).rstrip("/"),
            akg_api_key=env("AKG_API_KEY", ""),
            akg_session=env("AKG_SESSION", ""),
            akg_webhook_secret=env("AKG_WEBHOOK_SECRET", ""),
            cloud_token=env("WA_CLOUD_TOKEN", ""),
            cloud_phone_id=env("WA_CLOUD_PHONE_ID", ""),
            cloud_verify_token=env("WA_CLOUD_VERIFY_TOKEN", ""),
            cloud_app_secret=env("WA_CLOUD_APP_SECRET", ""),
            cloud_api_version=env("WA_CLOUD_API_VERSION", base.cloud_api_version),
            cloud_welcome_template=env("WA_CLOUD_WELCOME_TEMPLATE", ""),
            cloud_followup_template=env("WA_CLOUD_FOLLOWUP_TEMPLATE", ""),
            cloud_template_lang=env("WA_CLOUD_TEMPLATE_LANG", base.cloud_template_lang),
            followup_delays_hours=[
                float(x) for x in _list(env("FOLLOWUP_DELAYS_HOURS", "24,72"))
            ],
            followup_interval_seconds=int(env("FOLLOWUP_INTERVAL_SECONDS", "300")),
            send_gap_seconds=float(env("SEND_GAP_SECONDS", "5")),
            timezone=env("TIMEZONE", base.timezone),
            quiet_start_hour=int(env("SEND_WINDOW_START_HOUR", "9")),
            quiet_end_hour=int(env("SEND_WINDOW_END_HOUR", "19")),
        )
