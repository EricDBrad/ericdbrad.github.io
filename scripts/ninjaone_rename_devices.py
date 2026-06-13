#!/usr/bin/env python3
"""
NinjaOne bulk device rename script.

CSV format (devices.csv):
    device_id,new_name
    12345,WORKSTATION-CHICAGO-01
    12346,WORKSTATION-CHICAGO-02

Required environment variables (or set directly in a .env file):
    NINJA_CLIENT_ID      - OAuth2 client ID from NinjaOne
    NINJA_CLIENT_SECRET  - OAuth2 client secret from NinjaOne
    NINJA_INSTANCE       - Your NinjaOne instance hostname, e.g. app.ninjarmm.com
                           or yourcompany.rmmservice.com

Usage:
    python ninjaone_rename_devices.py devices.csv
    python ninjaone_rename_devices.py devices.csv --dry-run
    python ninjaone_rename_devices.py devices.csv --output results.csv
"""

import argparse
import csv
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLIENT_ID = os.getenv("NINJA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("NINJA_CLIENT_SECRET", "")
INSTANCE = os.getenv("NINJA_INSTANCE", "app.ninjarmm.com")

# NinjaOne API rate limit is 1200 req/min; stay well under it
REQUESTS_PER_SECOND = 5
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = [2, 4, 8]  # seconds between retries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Obtain a Bearer token via OAuth2 client_credentials grant."""
    url = f"https://{INSTANCE}/ws/oauth/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "monitoring management",
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {resp.text}")
    log.info("Authenticated successfully.")
    return token


# ---------------------------------------------------------------------------
# NinjaOne API calls
# ---------------------------------------------------------------------------

def get_device(session: requests.Session, device_id: int) -> dict:
    """Fetch current device info (used to confirm device exists)."""
    url = f"https://{INSTANCE}/v2/device/{device_id}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def rename_device(session: requests.Session, device_id: int, new_name: str) -> dict:
    """
    Rename a device by updating its displayName via PATCH /v2/device/{id}.
    Returns the updated device object.
    """
    url = f"https://{INSTANCE}/v2/device/{device_id}"
    payload = {"displayName": new_name}
    resp = session.patch(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    """Load device_id,new_name CSV. Returns list of dicts."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {"device_id", "new_name"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                f"CSV must have columns: {required}. Found: {reader.fieldnames}"
            )
        for i, row in enumerate(reader, start=2):
            did = row["device_id"].strip()
            name = row["new_name"].strip()
            if not did or not name:
                log.warning("Row %d: empty device_id or new_name — skipped.", i)
                continue
            if not did.isdigit():
                log.warning("Row %d: device_id '%s' is not numeric — skipped.", i, did)
                continue
            rows.append({"device_id": int(did), "new_name": name})
    return rows


def write_results(path: str, results: list[dict]) -> None:
    fieldnames = ["device_id", "new_name", "status", "old_name", "error"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


# ---------------------------------------------------------------------------
# Core rename loop
# ---------------------------------------------------------------------------

def process_renames(
    session: requests.Session,
    devices: list[dict],
    dry_run: bool,
) -> list[dict]:
    results = []
    total = len(devices)
    delay = 1.0 / REQUESTS_PER_SECOND

    for idx, device in enumerate(devices, start=1):
        device_id = device["device_id"]
        new_name = device["new_name"]
        log.info("[%d/%d] Device %d → '%s'", idx, total, device_id, new_name)

        old_name = None
        status = "unknown"
        error = ""

        # Fetch current name for logging
        for attempt in range(RETRY_ATTEMPTS):
            try:
                info = get_device(session, device_id)
                old_name = info.get("displayName") or info.get("systemName", "")
                break
            except requests.HTTPError as exc:
                if exc.response.status_code == 404:
                    log.warning("  Device %d not found — skipping.", device_id)
                    status = "not_found"
                    error = "404 Not Found"
                    break
                if attempt < RETRY_ATTEMPTS - 1:
                    wait = RETRY_BACKOFF[attempt]
                    log.warning("  Fetch error (%s), retrying in %ds…", exc, wait)
                    time.sleep(wait)
                else:
                    status = "error"
                    error = str(exc)
            except Exception as exc:
                status = "error"
                error = str(exc)
                break

        if status in ("not_found", "error"):
            results.append(
                {"device_id": device_id, "new_name": new_name,
                 "status": status, "old_name": old_name or "", "error": error}
            )
            time.sleep(delay)
            continue

        if dry_run:
            log.info("  [DRY RUN] Would rename '%s' → '%s'", old_name, new_name)
            results.append(
                {"device_id": device_id, "new_name": new_name,
                 "status": "dry_run", "old_name": old_name or "", "error": ""}
            )
            time.sleep(delay)
            continue

        # Perform rename with retry
        for attempt in range(RETRY_ATTEMPTS):
            try:
                rename_device(session, device_id, new_name)
                log.info("  Renamed '%s' → '%s'", old_name, new_name)
                status = "success"
                error = ""
                break
            except requests.HTTPError as exc:
                if exc.response.status_code in (400, 422):
                    log.error("  Rename rejected (%s) — not retrying.", exc)
                    status = "rejected"
                    error = f"{exc.response.status_code}: {exc.response.text[:200]}"
                    break
                if attempt < RETRY_ATTEMPTS - 1:
                    wait = RETRY_BACKOFF[attempt]
                    log.warning("  Rename error (%s), retrying in %ds…", exc, wait)
                    time.sleep(wait)
                else:
                    status = "error"
                    error = str(exc)
            except Exception as exc:
                status = "error"
                error = str(exc)
                break

        results.append(
            {"device_id": device_id, "new_name": new_name,
             "status": status, "old_name": old_name or "", "error": error}
        )
        time.sleep(delay)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk rename NinjaOne devices from a CSV.")
    parser.add_argument("csv_file", help="Path to CSV with device_id,new_name columns")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate and log what would happen without making API changes"
    )
    parser.add_argument(
        "--output", default="",
        help="Path for results CSV (default: results_<timestamp>.csv)"
    )
    args = parser.parse_args()

    # Validate config
    missing = [v for v in ("NINJA_CLIENT_ID", "NINJA_CLIENT_SECRET") if not os.getenv(v)]
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        log.error("Set them or create a .env file and run: eval $(cat .env | xargs) python ...")
        sys.exit(1)

    if not Path(args.csv_file).exists():
        log.error("CSV file not found: %s", args.csv_file)
        sys.exit(1)

    output_path = args.output or f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # Load devices
    devices = load_csv(args.csv_file)
    log.info("Loaded %d devices from %s", len(devices), args.csv_file)

    if args.dry_run:
        log.info("*** DRY RUN MODE — no changes will be made ***")

    # Auth
    token = get_access_token()
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })

    # Run
    results = process_renames(session, devices, dry_run=args.dry_run)

    # Summary
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info("Done. Results: %s", counts)

    write_results(output_path, results)
    log.info("Results written to %s", output_path)

    # Exit non-zero if any errors
    if counts.get("error", 0) + counts.get("rejected", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
