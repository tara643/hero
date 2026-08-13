"""
HERO Bikini Diver App -> REDCap Cloud
--------------------------------------
Replaces the n8n workflow (Webhook -> Validate -> Map fields -> POST -> respond)
with a single, plain-text Python service. One file, no hidden "live node state"
to drift out of sync with what's checked in here.

Endpoint:
    POST /webhook/hero-bikini
    Body: the JSON record the diver app's syncCurrentRecord() sends, e.g.
    {
      "record_id": "...", "submitted_at": "...", "app_version": "...",
      "mission": "Bikini Atoll", "code": "H01",
      "collection": "Baseline — Before we leave", "date": "2026-07-16",
      "notes": "...",
      "samples": [ { "name": "...", "by": "...", "done": true, "time": "14:32", "note": "" }, ... ]
    }

Config (set as environment variables, never hardcoded -- see README.md):
    REDCAP_API_TOKEN   -- the raw REDCap Cloud API token (the 'token' header value)
    REDCAP_BASE_URL    -- default: https://euuat.redcapcloud.com/rest/v2/import/records
    REDCAP_SITE_NAME   -- default: "Bikini Atoll"
    REDCAP_EVENT_NAME  -- default: "HERO BIKINI COLLECTION"
"""

import os
import re
import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hero-bikini-api")

# ---- Config -------------------------------------------------------------
REDCAP_API_TOKEN = os.environ.get("REDCAP_API_TOKEN", "")
REDCAP_BASE_URL = os.environ.get(
    "REDCAP_BASE_URL", "https://euuat.redcapcloud.com/rest/v2/import/records"
)
REDCAP_SITE_NAME = os.environ.get("REDCAP_SITE_NAME", "Bikini Atoll")
REDCAP_EVENT_NAME = os.environ.get("REDCAP_EVENT_NAME", "HERO BIKINI COLLECTION")

# ---- Sample mapping -------------------------------------------------------
# Sample title (as sent by the app) -> REDCap Cloud variable prefix + note field.
# Keep this in sync with the app's SAMPLES titles -- if a title in the app
# changes, update the matching key here (nothing else needs to change).
SAMPLE_FIELD_MAP: dict[str, dict[str, str]] = {
    "Blood — RNA tube (PAXgene)": {"prefix": "paxgene", "note_field": "paxgene_notes"},
    "Blood — cell preserver (K2EDTA + CryoGuard)": {"prefix": "cryo", "note_field": "cryo_note"},
    "Saliva sample": {"prefix": "spit", "note_field": "spit_note"},
    "Oral, Nasal, and Body Swabs": {"prefix": "swabs", "note_field": "swabs_notes"},
    "Stool sample": {"prefix": "stool", "note_field": "stool_notes"},
    # No urine sample for Bikini Atoll -- confirmed against the mission's
    # official collection protocol (PAXgene, Cryo, Saliva, Swabs, Stool only).
}

# "Collection / Timepoint" dropdown values, confirmed against the "Bikini Atoll"
# Response Set in REDCap Cloud: Baseline=1, Control Dives=2, Radiation Dives=3, Majuro=4.
TIMEPOINT_VALUE = {
    "baseline": 1,
    "control dives": 2,
    "radiation dives": 3,
    "majuro": 4,
}


def resolve_timepoint_value(collection_text: str | None) -> int | None:
    if not collection_text:
        return None
    name_part = re.split(r"[—-]", collection_text)[0].strip().lower()
    return TIMEPOINT_VALUE.get(name_part)


def combine_date_time(date: str | None, time: str | None) -> str:
    if not date or not time:
        return ""
    return f"{date} {time}"  # matches confirmed "YYYY-MM-DD HH:MM" format


def build_redcap_payload(record: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """Returns (redcap_payload_list, warnings)."""
    warnings: list[str] = []
    items: list[dict[str, str]] = []

    def push(name: str, value: Any) -> None:
        items.append({"itemName": name, "itemValue": "" if value is None else str(value)})

    push("participant_code", record.get("code", ""))
    push("mission_hero", record.get("mission", ""))
    push("date", record.get("date", ""))

    timepoint_value = resolve_timepoint_value(record.get("collection"))
    if timepoint_value is None and record.get("collection"):
        warnings.append(f"Could not match timepoint '{record.get('collection')}' to a known value")
    push("collection", timepoint_value if timepoint_value is not None else "")

    push("notes", record.get("notes", ""))

    all_done = True
    for sample in record.get("samples", []):
        mapping = SAMPLE_FIELD_MAP.get(sample.get("name", ""))
        if not mapping:
            warnings.append(f"Unmapped sample name: {sample.get('name')!r}")
            push(f"UNMAPPED_SAMPLE__{sample.get('name')}", str(sample))
            continue
        prefix, note_field = mapping["prefix"], mapping["note_field"]
        done = bool(sample.get("done"))
        push(f"{prefix}_done", "true" if done else "false")
        push(f"{prefix}_time", combine_date_time(record.get("date"), sample.get("time")))
        push(note_field, sample.get("note", ""))
        if not done:
            all_done = False

    # 2 = Complete, 1 = Data entry started (assumed convention, not yet
    # independently confirmed against REDCap Cloud's own definitions).
    push("HERO Bikini Collection_complete", "2" if all_done else "1")

    redcap_record = {
        "participantId": record.get("code", ""),
        "siteName": REDCAP_SITE_NAME,
        "eventName": REDCAP_EVENT_NAME,
        "eventDate": record.get("date", ""),
        "items": items,
    }
    return [redcap_record], warnings


# ---- App ------------------------------------------------------------------
app = FastAPI(title="HERO Bikini -> REDCap Cloud")

# Mirrors the n8n Webhook node's "Allowed Origins (CORS): *" setting.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def health() -> dict:
    return {"ok": True, "service": "hero-bikini-api"}


@app.post("/webhook/hero-bikini")
async def hero_bikini_webhook(request: Request) -> Response:
    try:
        record = await request.json()
    except Exception:
        log.warning("Received non-JSON body")
        return _json_response({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    if not record.get("code") or not record.get("date"):
        log.warning("Missing required fields: code and/or date. Payload: %s", record)
        return _json_response(
            {"ok": False, "error": "Missing required fields: code and/or date"}, status_code=400
        )

    if not REDCAP_API_TOKEN:
        log.error("REDCAP_API_TOKEN is not set -- refusing to call REDCap Cloud")
        return _json_response(
            {"ok": False, "error": "Server is not configured with a REDCap Cloud API token"},
            status_code=500,
        )

    payload, warnings = build_redcap_payload(record)
    if warnings:
        log.warning("Mapping warnings for %s: %s", record.get("code"), warnings)

    headers = {
        "Content-Type": "application/json",
        "accept": "application/json",
        "token": REDCAP_API_TOKEN,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(REDCAP_BASE_URL, json=payload, headers=headers)
    except httpx.RequestError as exc:
        log.error("Network error calling REDCap Cloud: %s", exc)
        return _json_response({"ok": False, "error": f"Network error: {exc}"}, status_code=502)

    if resp.status_code >= 300:
        log.error("REDCap Cloud rejected the record (%s): %s", resp.status_code, resp.text)
        return _json_response(
            {"ok": False, "error": "REDCap Cloud rejected the record", "detail": resp.text},
            status_code=502,
        )

    log.info("Successfully synced %s (%s)", record.get("code"), record.get("collection"))
    return _json_response({"ok": True, "record_id": record.get("record_id")}, status_code=200)


def _json_response(body: dict, status_code: int) -> Response:
    import json

    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
    )
