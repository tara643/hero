"""
HERO Study — REDCap Cloud Integration Backend
Single source of truth for mapping app payloads -> REDCap Cloud import records.

Rebuilt 2026-08-14 to mirror, field-for-field, a confirmed-working import
payload captured by exporting a UI-entered record (siteName "BioAstra",
eventName "Tara Mission(1)", crfOccurrence: 2, "{null}" for unset fields,
the "hero tara collection_complete" completion field, and participant/event
enrollment metadata). See CONFIRMED / CHANGED sections below for exactly
what was copied verbatim, what was intentionally left out, and why.

CONFIRMED (from REDCap Cloud UI / the captured curl, not guessed):
  - Site name:              "BioAstra"
  - Event display name:     "Tara Mission"
  - Event name on import is ALWAYS "Tara Mission(1)" -- fixed, not per-visit.
    (CORRECTED 2026-08-14 -- the event itself does not repeat per timepoint;
    what repeats per monthly visit is the CRF/instrument occurrence within
    that one event, i.e. crfOccurrence. Every item carries a matching
    "crfOccurrence": N for whichever timepoint (1-10) this submission is
    for. Explicit instruction, not inferred from the earlier curl capture --
    that capture's eventName "(1)" alongside crfOccurrence: 2 wasn't drift
    between two things that should agree; it was correct: event stays at
    (1), crfOccurrence was 2.)
  - REDCap Cloud API host:  https://eulogin.redcapcloud.com
  - Import endpoint:        POST /rest/v2/import/records
  - Every field on the instrument must be sent on every import, even unset
    ones, using the literal string "{null}" as itemValue.
  - The instrument's completion status is sent via itemName
    "hero tara collection_complete" (note: literal space before
    "_complete"), itemValue "2" for complete.

CHANGED 2026-08-14 (from the previous version of this file):

  1. "timepoint"'s value is now GUARANTEED to match crfOccurrence, because
     both come from the exact same integer -- resolve_occurrence() below,
     called once per submission. Previously (two revisions ago) these were
     resolved independently and could disagree; then (one revision ago)
     "timepoint" was removed entirely. Per direct feedback from REDCap
     Cloud support -- timepoint must match crfOccurrence -- the fix is to
     derive both from one source rather than either drop one or trust them
     to independently agree.

  2. Participant-enrollment fields (participantScreeningNumber,
     participantStatus, participantEnrollmentDate, participantScreeningDate,
     participantCreationDate) are only sent when crfOccurrence is 1
     (baseline), not copied onto every monthly submission. The captured
     curl included them on every occurrence because it was replaying a
     full record export; a real monthly submission doesn't have a genuine
     new enrollment date to report, and resending a fabricated "today" as
     the enrollment date on every visit would silently overwrite the real
     one each time. If this assumption is wrong, remove the
     `if occurrence == 1` guard in build_participant_metadata() below.

  3. Per-item "updatedBy"/"updatedDate" (present on almost every item in
     the captured curl) and "eventUpdatedBy"/"eventUpdatedDate" are NOT
     sent. These read as REDCap Cloud's own audit-log output from the
     export -- the same category of thing as the "responseSet" block that
     was attached to the old "timepoint" item (metadata describing the
     field, not data belonging to it). Fabricating "who last touched this"
     audit trails on import seemed worse than omitting them. The
     completion item's startedBy/startedDate/completedBy/completedDate ARE
     kept, since those look like they may be functionally meaningful to
     REDCap Cloud for closing out the instrument rather than pure audit
     trail -- populated with a generic app identity and the submission
     timestamp, since the app has no real crew-member name to put there.

  If none of the above turns out to matter and REDCap Cloud is fine either
  way, that's the best outcome; these are documented judgment calls made
  without a way to verify them ahead of a real submission, not claims that
  they're definitely required.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hero-api")

app = FastAPI(title="HERO Study REDCap Cloud Bridge")

# The app (HERO_TARA_Crew_App.html) runs in a mobile browser and calls this
# backend cross-origin. Without CORS middleware, browsers send an OPTIONS
# preflight before every POST, and FastAPI has no handler for it by
# default — resulting in every real submission being silently blocked
# with a 405, visible in Render logs as repeated
# "OPTIONS /webhook/hero-tara ... 405 Method Not Allowed" with no
# matching POST ever following. Confirmed 2026-08-14 in production logs.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
SITE_NAME_TARA = "BioAstra"
EVENT_NAME_TARA = "Tara Mission(1)"  # fixed -- always (1), see module docstring. crfOccurrence carries the visit number.
EVENT_STATUS_ACTIVE = "1"
PARTICIPANT_STATUS_ENROLLED = "1"

INSTRUMENT_COMPLETE_FIELD = "hero tara collection_complete"
STATUS_COMPLETE = "2"  # REDCap Cloud form-status code for "Complete"
APP_IDENTITY = "HERO TARA App"  # used for startedBy/completedBy; no real crew name available

# REDCap Cloud expects this literal string as itemValue for an unset field
# rather than the item being omitted. See module docstring.
NULL_VALUE = "{null}"

# resolve_occurrence()'s fallback for old/label-based callers or manual
# testing. The app now sends plain numeric timepoints ("1".."10") since
# HERO_TARA_Crew_App.html's timepoint labels were changed from
# "Baseline"/"Collection 2".."Home port" to plain numbers on 2026-08-14,
# so this table should rarely if ever be hit in normal operation.
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
}

# Fixed order of every sample field on the HERO TARA Collection instrument.
# Every one of these must appear in every import (see module docstring),
# even when the participant hasn't done that sample yet.
ALL_SAMPLE_PREFIXES = ["draw", "proc", "spit", "swabs", "stool", "urine"]

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
    timepoint: Optional[str] = None  # "1".."10" -- drives crfOccurrence only, see docstring
    timepoint_label: Optional[str] = None
    sub: Optional[str] = None
    date: str                       # "YYYY-MM-DD"
    notes: Optional[str] = None
    samples: list[SampleEntry] = []
    complete: Optional[bool] = True  # whether to mark the instrument Complete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resolve_occurrence(payload: TaraRecordPayload) -> int:
    """Map a submission to its REDCap Cloud CRF occurrence number.

    This is the single source of truth for which monthly visit a
    submission belongs to. It drives every item's crfOccurrence (and gates
    the baseline-only participant metadata block) -- it does NOT drive
    eventName, which is always fixed at "Tara Mission(1)" (see module
    docstring).

    Each of the app's 10 timepoints is one occurrence of the Repeating/
    Dynamic "Tara Mission" event, so occurrence = timepoint number. If the
    timepoint isn't a plain digit (e.g. an old label-based caller, or a
    missing timepoint), this can't be determined reliably -- log a warning
    and fall back to occurrence 1 rather than failing the request outright.
    """
    raw = (payload.timepoint or "").strip()
    if raw.isdigit():
        return int(raw)

    code = TIMEPOINT_CODES.get(raw.lower()) if raw else None
    if code is not None:
        return int(code)

    logger.warning(
        "Could not resolve a REDCap Cloud occurrence number from timepoint "
        "%r -- defaulting to occurrence 1. This will write to the wrong "
        "visit's data if that's not actually the first timepoint.",
        payload.timepoint,
    )
    return 1


def _item(name: str, value: Optional[str], occurrence: int) -> dict:
    """Build one REDCap Cloud item, always including crfOccurrence and
    always sending NULL_VALUE instead of omitting unset fields."""
    return {
        "itemName": name,
        "itemValue": value if value not in (None, "") else NULL_VALUE,
        "crfOccurrence": occurrence,
    }


def build_redcap_items(payload: TaraRecordPayload, occurrence: int) -> list[dict]:
    """Translate the app's payload into REDCap Cloud item objects.

    Field list confirmed 2026-08-14 directly against the HERO TARA
    Collection instrument: participant_code, mission_hero, date, timepoint,
    notes, then <sample>_done/_time/_notes for draw, proc, spit, swabs,
    stool, urine, then the instrument completion field.

    "timepoint"'s itemValue is str(occurrence) -- the exact same integer
    used for crfOccurrence on every item below, not a separately-resolved
    value. Per direct feedback from REDCap Cloud support: timepoint must
    match crfOccurrence. Deriving both from one number, in one place
    (resolve_occurrence()), makes that a guarantee rather than something
    that has to be kept in sync by hand.
    """
    items = [
        _item("participant_code", payload.code, occurrence),
        _item("mission_hero", payload.mission, occurrence),
        _item("date", payload.date, occurrence),
        _item("timepoint", str(occurrence), occurrence),
        _item("notes", payload.notes, occurrence),
    ]

    by_prefix = {}
    for sample in payload.samples:
        prefix = _resolve_prefix(sample)
        if not prefix:
            logger.warning("Could not map sample entry to a REDCap field: %s", sample)
            continue
        by_prefix[prefix] = sample

    for prefix in ALL_SAMPLE_PREFIXES:
        sample = by_prefix.get(prefix)
        done = bool(sample.done) if sample else False
        time_val = f"{payload.date} {sample.time}" if (sample and sample.time) else None
        note_val = sample.note if (sample and sample.note) else None

        items.append(_item(f"{prefix}_done", "true" if done else "false", occurrence))
        items.append(_item(f"{prefix}_time", time_val, occurrence))
        # Confirmed 2026-08-14 directly against the HERO TARA Collection
        # instrument: every sample type uses plural "_notes" (draw_notes,
        # proc_notes, spit_notes, swabs_notes, stool_notes, urine_notes).
        # No singular "_note" fields on this instrument.
        items.append(_item(f"{prefix}_notes", note_val, occurrence))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    items.append({
        "itemName": INSTRUMENT_COMPLETE_FIELD,
        "itemValue": STATUS_COMPLETE if payload.complete else "0",
        "crfOccurrence": occurrence,
        # See module docstring, change #3 -- kept unlike other items'
        # updatedBy/updatedDate because these look like they may be
        # functionally meaningful for closing out the instrument.
        "startedBy": APP_IDENTITY,
        "startedDate": now_iso,
        "completedBy": APP_IDENTITY,
        "completedDate": now_iso,
    })

    return items


def build_participant_metadata(payload: TaraRecordPayload, occurrence: int) -> dict:
    """Participant-enrollment fields from the confirmed-working curl.

    Only included on occurrence 1 (baseline) -- see module docstring,
    change #2, for why this isn't resent on every monthly submission.
    participantInitials is deliberately omitted: the app collects no
    crew-member name to derive it from, and putting something fabricated
    there seemed worse than leaving the field out.
    """
    if occurrence != 1:
        return {}

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    return {
        "participantScreeningNumber": payload.code,
        "participantStatus": PARTICIPANT_STATUS_ENROLLED,
        "participantEnrollmentDate": now_iso,
        "participantScreeningDate": today_iso,
        "participantCreationDate": now_iso,
    }


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
        # Flag every successful-looking call so nothing gets missed if the
        # historically-observed 200-with-empty-body silent failure recurs.
        "warning": (
            "REDCap Cloud returned 200 with an empty body. This endpoint has "
            "previously been observed to return 200 like this while writing "
            "nothing. Verify in the Subject Matrix -- do not assume success."
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
    # occurrence drives crfOccurrence on every item (and gates the baseline
    # participant-metadata block) -- it does NOT drive eventName anymore.
    # eventName is fixed at "Tara Mission(1)" regardless of which timepoint
    # this is. See module docstring.
    occurrence = resolve_occurrence(payload)

    record = {
        "participantId": payload.code,
        **build_participant_metadata(payload, occurrence),
        "siteName": SITE_NAME_TARA,
        "eventName": EVENT_NAME_TARA,
        "eventDate": payload.date,
        "eventStatus": EVENT_STATUS_ACTIVE,
        "items": build_redcap_items(payload, occurrence),
    }

    logger.info(
        "Posting TARA record for participant %s, occurrence %s: %s",
        payload.code, occurrence, record,
    )
    result = post_to_redcap([record])
    # Debugging visibility, not needed for REDCap Cloud itself: lets the
    # app (or anyone testing with curl/Postman) confirm what this backend
    # actually computed and sent, without needing Render log access.
    result["debug"] = {
        "timepoint_selected": payload.timepoint,
        "crf_occurrence_used": occurrence,
        "event_name_sent": EVENT_NAME_TARA,
        "participant_metadata_included": bool(build_participant_metadata(payload, occurrence)),
    }
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
