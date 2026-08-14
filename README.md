# HERO TARA API Bridge

Rebuilt 2026-08-14. Bikini pipeline retired (mission concluded) — this
service now only supports the TARA Polaris mission, against the single
unified REDCap Cloud study:

**HERO : Humans in Extreme Environments Research Omics**

## Confirmed study facts (source of truth for this rebuild)

| Item | Value |
|---|---|
| Site name | `HERO-1 - BioAstra` |
| Event display name | `Tara Mission` |
| Event short / unique name | `tara_mission` |
| Instruments on this event | HERO TARA Collection, HERO TARA Station, Illness or Injury, Medications & Supplements |
| REDCap Cloud API host | `https://eulogin.redcapcloud.com` |
| Import endpoint | `POST /rest/v2/import/records` |
| Auth header | `token: <raw token>` (no "Bearer") |

## Environment variables (set in Render, never in code)

- `REDCAP_API_TOKEN` — study-level API token, generated inside this
  specific study (not the old deprecated "TESTING FOR BIKINI ATOLL" study).
- `REDCAP_BASE_URL` — defaults to `https://eulogin.redcapcloud.com/rest/v2/import/records`
  in code, but can be overridden here if REDCap Cloud support tells us
  to use a different host.

## Deploy notes — Python version pinned

`runtime.txt` pins Render to **Python 3.12.7**. Do not remove this or
let it drift to a newer default. On 2026-08-14 a build against Python
3.14 failed because `pydantic-core` (a Rust extension `pydantic`
depends on) has no prebuilt wheel for 3.14 yet — pip fell back to
compiling it from source via `maturin`, which then failed because
Render's build sandbox has a read-only Cargo registry cache path.
This is unrelated to the REDCap integration logic; it's purely a
"don't build on a Python version newer than your dependencies support"
problem. `requirements.txt` also pins `pydantic==2.10.3` /
`fastapi==0.115.6`, both confirmed to have prebuilt `cp312` wheels.

## Known unresolved issue — READ BEFORE TRUSTING A 200

As of 2026-08-14, `POST /rest/v2/import/records` against this study
returns **HTTP 200 with an empty array `[]`** and does **not** write any
data, verified directly against the Subject Matrix (TEST01, Tara Mission
column stayed "not started" before and after). This was reproduced with:

- `eventName` as both `"Tara Mission"` (display label) and `"tara_mission"`
  (short/unique name) — no difference
- With and without `crfOccurrence: 1` on each item (Tara Mission is a
  repeating event) — no difference
- Via Swagger UI and via raw `curl`, bypassing all app/backend code — no difference

This means **a 200 response from this backend is not proof the data was
saved.** The `warning` field in this backend's response flags this
automatically when REDCap Cloud's body comes back empty. A support ticket
is pending with REDCap Cloud/nPhase (see `SUPPORT_TICKET.md`).

**Do not remove the `warning` field or treat 200 as success until this is
resolved and confirmed via a real write showing up in the Subject Matrix.**

## Endpoints

- `POST /webhook/hero-tara` — accepts the payload shape sent by
  `HERO_TARA_Crew_App.html`'s `record()` function and maps it to REDCap
  Cloud's import format.
- `POST /webhook/hero-tara-station` — not yet wired up (501). Station uses
  `participant_code_station` as a separate field from Collection's
  `participant_code`, per REDCap Cloud's study-wide unique-variable-name
  rule.

## Field mapping notes

Confirmed 2026-08-14 directly against the **HERO TARA Collection**
instrument's actual field list (not the PDF export, not memory):

```
participant_code, mission_hero, date, timepoint, notes,
draw_done / draw_time / draw_notes,
proc_done / proc_time / proc_notes,
spit_done / spit_time / spit_notes,
swabs_done / swabs_time / swabs_notes,
stool_done / stool_time / stool_notes,
urine_done / urine_time / urine_notes
```

Every sample type uses **plural** `_notes` — there is no singular
`_note` field on this instrument (an earlier draft of this file guessed
wrong on `spit`/`proc`; fixed).

The app's internal sample id for urine is `pee` (legacy naming from an
earlier draft) but the REDCap Cloud variable is `urine_done` /
`urine_time` / `urine_notes`. This backend is the single place that
translation happens — do not "fix" the app to say `urine` internally
without also checking this file, and vice versa.

**`timepoint` field is numeric-coded, and labels have inconsistent
casing.** Confirmed the REDCap Cloud `timepoint` field expects `"1"`
through `"10"`, not the label text. The app's own timepoint strings
(`"Baseline"`, `"Collection 2"`..`"Collection 9"`, `"Home port"`) don't
even match the instrument PDF's label casing (`"Home Port"`) — the
lookup in `main.py` is case-insensitive specifically to survive this.

Sample-id → REDCap field prefix map lives in `SAMPLE_FIELD_PREFIX` in
`main.py`. Update it there if the study's instrument variable names ever
change (they have before — see the `tara_mission` vs `Tara Mission`
mixup this rebuild grew out of).

## Deploy

Push to `main` on `github.com/tara643/hero` — Render auto-redeploys.
Free tier cold start is ~30–50s; the app's offline queue/retry logic
already handles this.
