"""Command line helpers.

python -m leadflow.cli import contacts.csv   Import leads (columns: name, phone, email,
                                             company, service, message, consent)
python -m leadflow.cli followups             Run one follow-up pass now
"""

from __future__ import annotations

import argparse
import csv
import sys
import time

from .config import Settings, load_dotenv
from .db import Database
from .providers import build_provider
from .service import LeadService

YES = {"1", "y", "yes", "true"}


def import_csv(service: LeadService, path: str, gap: float) -> tuple[int, int]:
    imported = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            consent = row.get("consent", "").lower() in YES
            try:
                service.capture_lead(
                    phone=row.get("phone", ""),
                    name=row.get("name", ""),
                    email=row.get("email", ""),
                    company=row.get("company", ""),
                    service=row.get("service", ""),
                    message=row.get("message", ""),
                    consent=consent,
                    source="import",
                )
            except ValueError:
                skipped += 1
                continue
            imported += 1
            if consent and gap:
                time.sleep(gap)  # pace outgoing welcomes
    return imported, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadflow")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("followups", help="run one follow-up pass")
    importer = commands.add_parser("import", help="import leads from a CSV file")
    importer.add_argument("path")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    service = LeadService(settings, Database(settings.db_path), build_provider(settings))
    if args.command == "import":
        imported, skipped = import_csv(service, args.path, settings.send_gap_seconds)
        print(f"Imported {imported} leads, skipped {skipped} with invalid phone numbers.")
        print("Only rows with consent=yes were messaged; the rest are stored as prospects.")
    else:
        print(f"Sent {service.run_followups()} follow-up messages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
