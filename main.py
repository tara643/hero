"""
HERO Study — REDCap Cloud Integration Backend
Single source of truth for mapping app payloads -> REDCap Cloud import records.

Rebuilt from scratch on 2026-08-14 after the old "HERO Bikini Collection"
pipeline was retired (Bikini mission is over) and the study consolidated
into one unified REDCap Cloud study:
  "HERO : Humans in Extreme Environments Research Omics"

CONFIRMED (from REDCap Cloud UI, not guessed):
  - Site name:              "HERO-1 - BioAstra"
  - Event display name:     "Tara Mission"
  - Event short/unique name:"tara_mission"
  - Instruments on event:   HERO TARA Collection, HERO TARA Station,
                             Illness or Injury, Medications & Supplements
  - REDCap Cloud API host:  https://eulogin.redcapcloud.com
      (NOT euuat.redcapcloud.com -- confirmed via raw curl, this is what
       actually accepted the token without a 401)
  - Import endpoint:        POST /rest/v2/import/records

**KNOWN UNRESOLVED ISSUE (as of 2026-08-14):**
  A well-formed POST to /rest/v2/import/records against this study
  currently returns HTTP 200 with an empty array `[]` and does NOT
  write any data (verified against the Subject Matrix before/after).
  This was tested with both eventName="Tara Mission" and
  eventName="tara_mission", with and without crfOccurrence on items,
  via both Swagger and raw curl -- same empty-array result every time.
  This is NOT a bug in this file. Until REDCap Cloud/nPhase support
  identifies the missing piece, calls made through this backend will
  likely also return 200 without persisting data. See SUPPORT_TICKET.md.
"""

import os
import logging
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hero-api")

app = FastAPI(title="HERO Study REDCap Cloud Bridge")

# ---------------------------------------------------------------------------
# Config -- values live in Render environment variables, never hardcoded here
# ---------------------------------------------------------------------------
REDCAP_API_TOKEN = os.environ.get("REDCAP_API_TOKEN")
REDCAP_BASE_URL = os.environ.get(
    "REDCAP_BASE_URL", "https://eulogin.redcapcloud.com/rest/v2/import/records"
)

if not REDCAP_API_TOKEN:
    logger.warning("REDCAP_API_TOKEN is not set — imports will fail with 401.")

# ---------------------------------------------------------------------------
# Study constants
# ---------------------------------------------------------------------------
SITE_NAME_TARA = "HERO-1 - BioAstra"
EVENT_NAME_TARA = "tara_mission"  # unique/short event name, not the display label

# HERO TARA Collection's "timepoint" field is a dropdown/radio field.
# REDCap Cloud stores the numeric Response Set value, not the label text
# (confirmed 2026-08-14 — same pattern as the Bikini instrument's
# "collection" field documented in earlier sessions). Sending the literal
# label ("Baseline") instead of "1" would silently fail exactly like the
# eventName label/short-name mismatch did.
#
# NOTE: the app's actual timepoint strings (from HERO_TARA_Crew_App.html's
# MISSION.timepoints) are "Baseline", "Collection 2".."Collection 9", and
# "Home port" (lowercase "p") -- NOT "Home Port" as shown in the exported
# instrument PDF. Lookup is case-insensitive below specifically so this
# kind of label-casing drift can't cause another silent failure.
TIMEPOINT_CODES = {
    "baseline": "1",
    "collection 2": "2",
    "collection 3": "3",
    "collection 4": "4",
    "collection 5": "5",
    "collection 6": "6",
    "collection 7": "7",
    "collection 8": "8",
    "collection 9": "9",
    "home port": "10",
}

# Internal app sample-id -> REDCap Cloud variable-name prefix.
# NOTE: the app's internal id for urine is "pee" for historical reasons;
# the REDCap Cloud variable name is "urine_*", not "pee_*". This mapping
# is the single place that translation happens.
SAMPLE_FIELD_PREFIX = {
    "draw": "draw",
    "proc": "proc",
    "spit": "spit",
    "swabs": "swabs",
    "stool": "stool",
    "pee": "urine",
    "station": "station",
}

# ---------------------------------------------------------------------------
# Request models -- shape sent by HERO_TARA_Crew_App.html's record() function
# ---------------------------------------------------------------------------
class SampleEntry(BaseModel):
    name: Optional[str] = None
    by: Optional[str] = None
    done: Optional[bool] = False
    time: Optional[str] = None      # e.g. "14:32" (local time picked in app)
    note: Optional[str] = None
    photo_attached: Optional[bool] = False
    photo: Optional[str] = None     # not sent to REDCap; app-side only for now


class TaraRecordPayload(BaseModel):
    mission: Optional[str] = None
    code: str                       # participant code, e.g. "H0100"
    timepoint: Optional[str] = None
    timepoint_label: Optional[str] = None
    sub: Optional[str] = None
    date: str                       # "YYYY-MM-DD"
    notes: Optional[str] = None
    samples: list[SampleEntry] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_redcap_items(payload: TaraRecordPayload) -> list[dict]:
    """Translate the app's sample list into REDCap Cloud item objects.

    Confirmed 2026-08-14 directly against the HERO TARA Collection
    instrument's field list: participant_code, mission_hero, date,
    timepoint, notes, then <sample>_done/_time/_notes for draw, proc,
    spit, swabs, stool, urine.
    """
    items = [
        {"itemName": "participant_code", "itemValue": payload.code},
        {"itemName": "date", "itemValue": payload.date},
    ]

    if payload.mission:
        items.append({"itemName": "mission_hero", "itemValue": payload.mission})

    if payload.timepoint:
        code = TIMEPOINT_CODES.get(payload.timepoint.strip().lower())
        if code is None:
            logger.warning(
                "Unrecognized timepoint label %r — sending as-is, but this "
                "will likely silently fail if REDCap Cloud expects a numeric "
                "Response Set code. Add it to TIMEPOINT_CODES.",
                payload.timepoint,
            )
        items.append({"itemName": "timepoint", "itemValue": code or payload.timepoint})

    if payload.notes:
        items.append({"itemName": "notes", "itemValue": payload.notes})

    for sample in payload.samples:
        # figure out which app-internal id this entry corresponds to.
        # The app's record() doesn't send the raw id, only "name" (display
        # title like "Blood Draw"), so we match on name -> id via a small
        # reverse lookup built from SAMPLES titles used in the app.
        prefix = _resolve_prefix(sample)
        if not prefix:
            logger.warning("Could not map sample entry to a REDCap field: %s", sample)
            continue

        items.append({
            "itemName": f"{prefix}_done",
            "itemValue": "true" if sample.done else "false",
        })
        if sample.time:
            items.append({
                "itemName": f"{prefix}_time",
                "itemValue": f"{payload.date} {sample.time}",
            })
        if sample.note:
            # Confirmed 2026-08-14 directly against the HERO TARA Collection
            # instrument: every sample type uses plural "_notes" (draw_notes,
            # proc_notes, spit_notes, swabs_notes, stool_notes, urine_notes).
            # No singular "_note" fields on this instrument.
            items.append({"itemName": f"{prefix}_notes", "itemValue": sample.note})

    return items


# Display titles used in HERO_TARA_Crew_App.html's SAMPLES object, mapped
# back to internal ids. Kept here (rather than trusting the app to send
# the id directly) so this file stays the single source of truth even if
# the app's copy changes.
_TITLE_TO_ID = {
    "Blood Draw": "draw",
    "EDTA Tube Processing": "proc",
    "Saliva Collection": "spit",
    "Oral, Nasal & Body Swabs": "swabs",
    "Stool Collection": "stool",
    "Urine Collection": "pee",
    "Station Surfaces": "station",
}


def _resolve_prefix(sample: SampleEntry) -> Optional[str]:
    internal_id = _TITLE_TO_ID.get(sample.name or "")
    if not internal_id:
        return None
    return SAMPLE_FIELD_PREFIX.get(internal_id)


def post_to_redcap(record_body: list[dict]) -> dict:
    if not REDCAP_API_TOKEN:
        raise HTTPException(status_code=500, detail="REDCAP_API_TOKEN not configured on server.")

    headers = {
        "accept": "application/json",
        "token": REDCAP_API_TOKEN,
        "content-type": "application/json",
    }

    resp = requests.post(REDCAP_BASE_URL, headers=headers, json=record_body, timeout=30)

    logger.info("REDCap Cloud response: %s %s", resp.status_code, resp.text[:500])

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return {
        "redcap_status": resp.status_code,
        "redcap_body": resp.text,
        # Flag every successful-looking call so nothing gets missed while
        # the empty-array issue (see module docstring) is unresolved.
        "warning": (
            "REDCap Cloud returned 200. As of 2026-08-14 this endpoint has "
            "been observed to return 200 with an empty body without actually "
            "writing data. Verify in the Subject Matrix, do not assume success."
        ) if resp.text.strip() in ("[]", "") else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok", "service": "hero-tara-api"}


@app.post("/webhook/hero-tara")
def webhook_hero_tara(payload: TaraRecordPayload):
    record_body = [{
        "participantId": payload.code,
        "siteName": SITE_NAME_TARA,
        "eventName": EVENT_NAME_TARA,
        "eventDate": payload.date,
        "items": build_redcap_items(payload),
    }]

    logger.info("Posting TARA record for participant %s: %s", payload.code, record_body)
    result = post_to_redcap(record_body)
    return result


@app.post("/webhook/hero-tara-station")
def webhook_hero_tara_station(request: Request):
    # Kept as a minimal pass-through until the Station-specific app flow
    # is wired up. Station uses a separate participant_code_station field
    # per REDCap Cloud's study-wide unique-variable-name requirement.
    raise HTTPException(
        status_code=501,
        detail="Station webhook not yet wired to the rebuilt pipeline.",
    )
