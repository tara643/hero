"""
HERO Diver/Crew Apps -> REDCap Cloud
--------------------------------------
Replaces the n8n workflow with a single, plain-text Python service covering
both missions now that they share one unified REDCap Cloud study ("HERO:
Humans in Extreme Environments Research Omics"). One file, no hidden "live
node state" to drift out of sync with what's checked in here.

Endpoints:
    POST /webhook/hero-bikini         -- Bikini diver app -> HERO Bikini Collection
    POST /webhook/hero-tara           -- TARA crew app (regular collections) -> HERO TARA Collection
    POST /webhook/hero-tara-station   -- TARA crew app (Station Surfaces) -> HERO TARA Station

Config (set as environment variables, never hardcoded -- see README.md):
    REDCAP_API_TOKEN   -- the raw REDCap Cloud API token (the 'token' header value).
                          One token, since Bikini and TARA are now the same study.
    REDCAP_BASE_URL    -- default: https://euuat.redcapcloud.com/rest/v2/import/records
    REDCAP_BIKINI_SITE_NAME    -- default: "Bikini Atoll"
    REDCAP_BIKINI_EVENT_NAME   -- default: "HERO BIKINI COLLECTION"
    REDCAP_TARA_SITE_NAME      -- default: "HERO-1 - BioAstra" (confirmed)
    REDCAP_TARA_EVENT_NAME     -- default: "Tara Mission" (confirmed Event Definition name)
"""

import os
import re
import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hero-api")

# ---- Config -------------------------------------------------------------
REDCAP_API_TOKEN = os.environ.get("REDCAP_API_TOKEN", "")
REDCAP_BASE_URL = os.environ.get(
    "REDCAP_BASE_URL", "https://euuat.redcapcloud.com/rest/v2/import/records"
)
REDCAP_BIKINI_SITE_NAME = os.environ.get("REDCAP_BIKINI_SITE_NAME", "Bikini Atoll")
REDCAP_BIKINI_EVENT_NAME = os.environ.get("REDCAP_BIKINI_EVENT_NAME", "HERO BIKINI COLLECTION")
# TODO: confirm the exact Site name for TARA in REDCap Cloud (Study -> Sites) --
# this is a placeholder guess and must be checked before going live.
REDCAP_TARA_SITE_NAME = os.environ.get("REDCAP_TARA_SITE_NAME", "HERO-1 - BioAstra")
REDCAP_TARA_EVENT_NAME = os.environ.get("REDCAP_TARA_EVENT_NAME", "Tara Mission")

# ---- Bikini sample mapping -------------------------------------------------
# Sample title (as sent by the app) -> REDCap Cloud variable prefix + note field.
BIKINI_SAMPLE_FIELD_MAP: dict[str, dict[str, str]] = {
    "Blood — RNA tube (PAXgene)": {"prefix": "paxgene", "note_field": "paxgene_notes"},
    "Blood — cell preserver (K2EDTA + CryoGuard)": {"prefix": "cryo", "note_field": "cryo_note"},
    "Saliva sample": {"prefix": "spit", "note_field": "spit_note"},
    "Oral, Nasal, and Body Swabs": {"prefix": "swabs", "note_field": "swabs_notes"},
    "Stool sample": {"prefix": "stool", "note_field": "stool_notes"},
    # No urine sample for Bikini Atoll -- confirmed against the mission's
    # official collection protocol (PAXgene, Cryo, Saliva, Swabs, Stool only).
}

BIKINI_TIMEPOINT_VALUE = {
    "baseline": 1,
    "control dives": 2,
    "radiation dives": 3,
    "majuro": 4,
}

# ---- TARA sample mapping ---------------------------------------------------
# All six prefixes confirmed directly: draw, proc, spit, swabs, stool, urine.
TARA_SAMPLE_FIELD_MAP: dict[str, dict[str, str]] = {
    "Blood Draw": {"prefix": "draw", "note_field": "draw_notes"},
    "EDTA Tube Processing": {"prefix": "proc", "note_field": "proc_notes"},
    "Saliva Collection": {"prefix": "spit", "note_field": "spit_notes"},
    "Oral, Nasal & Body Swabs": {"prefix": "swabs", "note_field": "swabs_notes"},
    "Stool Collection": {"prefix": "stool", "note_field": "stool_notes"},
    "Urine Collection": {"prefix": "urine", "note_field": "urine_notes"},
}

# Timepoint dropdown values, confirmed: Baseline=1, Collection 2..9 = 2..9, Home port=10.
TARA_TIMEPOINT_VALUE = {
    "baseline": 1,
    "collection 2": 2,
    "collection 3": 3,
    "collection 4": 4,
    "collection 5": 5,
    "collection 6": 6,
    "collection 7": 7,
    "collection 8": 8,
    "collection 9": 9,
    "home port": 10,
}


def resolve_timepoint_value(timepoint_map: dict[str, int], text: str | None) -> int | None:
    if not text:
        return None
    name_part = re.split(r"[—-]", text)[0].strip().lower()
    return timepoint_map.get(name_part)


def combine_date_time(date: str | None, time: str | None) -> str:
    if not date or not time:
        return ""
    return f"{date} {time}"  # matches confirmed "YYYY-MM-DD HH:MM" format


def build_redcap_payload(
    record: dict[str, Any],
    *,
    sample_field_map: dict[str, dict[str, str]],
    timepoint_map: dict[str, int] | None,
    timepoint_source_field: str,
    timepoint_target_field: str,
    site_name: str,
    event_name: str,
    complete_field_name: str,
    top_level_fields: dict[str, str],
) -> tuple[list[dict], list[str]]:
    """Generic payload builder shared by all three routes.

    top_level_fields maps REDCap variable name -> key to read from `record`
    (e.g. {"participant_code": "code", "mission_hero": "mission"}).
    timepoint_target_field is the REDCap variable name the resolved timepoint
    value gets written to (e.g. "collection" for Bikini, "timepoint" for TARA).
    """
    warnings: list[str] = []
    items: list[dict[str, str]] = []

    def push(name: str, value: Any) -> None:
        items.append({"itemName": name, "itemValue": "" if value is None else str(value)})

    for redcap_var, record_key in top_level_fields.items():
        push(redcap_var, record.get(record_key, ""))

    if timepoint_map is not None:
        raw = record.get(timepoint_source_field)
        timepoint_value = resolve_timepoint_value(timepoint_map, raw)
        if timepoint_value is None and raw:
            warnings.append(f"Could not match timepoint {raw!r} to a known value")
        push(timepoint_target_field, timepoint_value if timepoint_value is not None else "")

    all_done = True
    for sample in record.get("samples", []):
        mapping = sample_field_map.get(sample.get("name", ""))
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

    push(complete_field_name, "2" if all_done else "1")

    redcap_record = {
        "participantId": record.get("code", ""),
        "siteName": site_name,
        "eventName": event_name,
        "eventDate": record.get("date", ""),
        "items": items,
    }
    return [redcap_record], warnings


def build_bikini_payload(record: dict[str, Any]) -> tuple[list[dict], list[str]]:
    payload, warnings = build_redcap_payload(
        record,
        sample_field_map=BIKINI_SAMPLE_FIELD_MAP,
        timepoint_map=BIKINI_TIMEPOINT_VALUE,
        timepoint_source_field="collection",
        timepoint_target_field="collection",
        site_name=REDCAP_BIKINI_SITE_NAME,
        event_name=REDCAP_BIKINI_EVENT_NAME,
        complete_field_name="HERO Bikini Collection_complete",
        top_level_fields={
            "participant_code": "code",
            "mission_hero": "mission",
            "date": "date",
            "notes": "notes",
        },
    )
    return payload, warnings


def build_tara_collection_payload(record: dict[str, Any]) -> tuple[list[dict], list[str]]:
    payload, warnings = build_redcap_payload(
        record,
        sample_field_map=TARA_SAMPLE_FIELD_MAP,
        timepoint_map=TARA_TIMEPOINT_VALUE,
        timepoint_source_field="timepoint",  # TARA app's record() uses 'timepoint', not 'collection'
        timepoint_target_field="timepoint",
        site_name=REDCAP_TARA_SITE_NAME,
        event_name=REDCAP_TARA_EVENT_NAME,
        complete_field_name="HERO TARA Collection_complete",
        top_level_fields={
            "participant_code": "code",
            "mission_hero": "mission",
            "date": "date",
            "notes": "notes",
        },
    )
    return payload, warnings


def build_tara_station_payload(record: dict[str, Any]) -> tuple[list[dict], list[str]]:
    # Station Surfaces has just one sample type and no timepoint dropdown.
    # Confirmed: participant field is named participant_code_station (chosen to
    # avoid the study-wide uniqueness conflict with the Collection instrument's
    # participant_code field).
    payload, warnings = build_redcap_payload(
        record,
        sample_field_map={"Station surfaces": {"prefix": "station", "note_field": "station_notes"}},
        timepoint_map=None,
        timepoint_source_field="",
        timepoint_target_field="",
        site_name=REDCAP_TARA_SITE_NAME,
        event_name=REDCAP_TARA_EVENT_NAME,
        complete_field_name="HERO TARA Station_complete",
        top_level_fields={
            "participant_code_station": "code",
            "date": "date",
        },
    )
    return payload, warnings


# ---- App ------------------------------------------------------------------
app = FastAPI(title="HERO -> REDCap Cloud")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def health() -> dict:
    return {"ok": True, "service": "hero-api"}


async def _handle_webhook(request: Request, builder) -> Response:
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

    payload, warnings = builder(record)
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

    log.info("Successfully synced %s", record.get("code"))
    return _json_response({"ok": True, "record_id": record.get("record_id")}, status_code=200)


@app.post("/webhook/hero-bikini")
async def hero_bikini_webhook(request: Request) -> Response:
    return await _handle_webhook(request, build_bikini_payload)


@app.post("/webhook/hero-tara")
async def hero_tara_webhook(request: Request) -> Response:
    return await _handle_webhook(request, build_tara_collection_payload)


@app.post("/webhook/hero-tara-station")
async def hero_tara_station_webhook(request: Request) -> Response:
    return await _handle_webhook(request, build_tara_station_payload)


def _json_response(body: dict, status_code: int) -> Response:
    import json

    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
    )

