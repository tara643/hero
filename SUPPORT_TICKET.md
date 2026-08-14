Subject: POST /v2/import/records returns 200 with empty array, writes no data

Study: HERO : Humans in Extreme Environments Research Omics
Instance: eulogin.redcapcloud.com
Endpoint: POST /rest/v2/import/records
User role: Super Admin (Tara Sangal)

## Summary

A well-formed import request to /v2/import/records consistently returns
HTTP 200 with body `[]` and does not persist any data, verified against
the Subject Matrix before and after each call.

## Steps to reproduce

1. Enroll a test participant (TEST01) at Site "HERO-1 - BioAstra" via the
   normal Enroll UI. Confirm via Subject Matrix that the "Tara Mission"
   event column shows the not-started icon.
2. Send the following via curl (token omitted here, used a fresh
   study-level token generated inside this study, confirmed via a
   successful GET /v2/export/records/{siteId} call with the same token):

```
curl -X POST "https://eulogin.redcapcloud.com/rest/v2/import/records" \
  -H "accept: application/json" \
  -H "token: <redacted>" \
  -H "content-type: application/json" \
  -d '[{"participantId":"TEST01","siteName":"HERO-1 - BioAstra","eventName":"tara_mission","eventDate":"2026-08-14","items":[{"itemName":"participant_code","itemValue":"TEST01"},{"itemName":"draw_done","itemValue":"true"},{"itemName":"draw_time","itemValue":"2026-08-14 12:00"}]}]'
```

3. Response: `200 OK`, body `[]`.
4. Refresh the Subject Matrix for TEST01 — the "Tara Mission" column is
   still showing "not started." No data was written.

## What we've already ruled out

- **Auth/token validity** — same token succeeds on a GET call
  (`/v2/export/records/{siteId}`) against the same study, returning `200`
  with a valid (empty, since no data exists yet) records array.
- **Event name mismatch** — tried both the event's display name
  ("Tara Mission") and its Unique Event Name ("tara_mission"). Both
  produce the identical `[]` response.
- **Repeating-event occurrence** — "Tara Mission" is a repeating event.
  Added `"crfOccurrence": 1` to every item object. No change in response.
- **Malformed body** — the same request shape (participantId, siteName,
  eventName, eventDate, items[] with itemName/itemValue) is documented
  as working in this study's own Swagger example, and was previously
  confirmed to write successfully against an earlier, now-deprecated test
  study using the same pattern.
- **Domain confusion** — reproduced via raw curl against
  eulogin.redcapcloud.com directly, bypassing Swagger, our backend, and
  our app entirely.

## What we have not been able to test

- Whether manual data entry through the study's own UI succeeds for this
  same event/instrument (in progress).
- Whether an instance-level vs. study-level token distinction is relevant
  here (per your documentation's Section 1 vs Section 2, which we have
  not yet had explained to us in this context).

## Ask

Given that GET/export works cleanly with this token and study, but every
POST/import variant we've tried returns 200 with no write, we suspect
either (a) an undocumented required field for this endpoint on repeating
events, or (b) a study/instance-side configuration issue specific to this
unified study (as opposed to our old deprecated test study, where the
identical request pattern worked). Could you advise which, and whether
there's a way to get import errors surfaced instead of a silent empty
200?
