# HERO Bikini -> REDCap Cloud (Python service)

Replaces the n8n workflow with a single FastAPI app. One file (`main.py`) holds
all the logic that used to be split across the Webhook / Validate / Map fields /
POST nodes in n8n.

## What it does

`POST /webhook/hero-bikini` — receives the JSON record the diver app sends,
maps it to REDCap Cloud's `items` format, and forwards it to REDCap Cloud's
import API. Returns `{"ok": true}` or `{"ok": false, "error": "..."}`.

## 1. Run it locally first

```bash
cd hero_bikini_api
pip install -r requirements.txt

export REDCAP_API_TOKEN="your-real-token-here"
# Optional overrides (these are already the right defaults for Bikini):
# export REDCAP_BASE_URL="https://euuat.redcapcloud.com/rest/v2/import/records"
# export REDCAP_SITE_NAME="Bikini Atoll"
# export REDCAP_EVENT_NAME="HERO BIKINI COLLECTION"

uvicorn main:app --reload --port 8000
```

Visit `http://localhost:8000/` in a browser — you should see
`{"ok": true, "service": "hero-bikini-api"}`. That confirms the server itself
is running before you test against the real REDCap Cloud API.

Test a real submission from the command line:

```bash
curl -X POST http://localhost:8000/webhook/hero-bikini \
  -H "Content-Type: application/json" \
  -d '{
    "code": "TEST99",
    "mission": "Bikini Atoll",
    "date": "2026-07-21",
    "collection": "Baseline — Before we leave",
    "notes": "local test",
    "samples": [
      {"name": "Blood — RNA tube (PAXgene)", "by": "medic", "done": true, "time": "12:00", "note": ""}
    ]
  }'
```

Check REDCap Cloud's Subject Matrix afterward to confirm it landed, same as
we did when testing through Swagger.

## 2. Deploy it so the app can actually reach it

The diver app needs a real, always-on URL — this can't run only on your
laptop. **Render** is a good fit: free tier, simple deploys, no server
management.

1. Push this folder to a GitHub repo (or use Render's manual deploy if you'd
   rather not use git).
2. In Render: **New -> Web Service**, connect the repo.
3. **Build command:** `pip install -r requirements.txt`
4. **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Under **Environment**, add:
   - `REDCAP_API_TOKEN` = your real token (rotate it first if it's ever been
     shared anywhere, same rule as before)
   - Leave `REDCAP_BASE_URL`, `REDCAP_SITE_NAME`, `REDCAP_EVENT_NAME` unset
     unless you need to override the Bikini defaults baked into `main.py`.
6. Deploy. Render gives you a URL like `https://hero-bikini-api.onrender.com`.

## 3. Point the diver app at it

In `HERO_Bikini_Diver_App.html`, update:

```js
var WEBHOOK_URL = 'https://hero-bikini-api.onrender.com/webhook/hero-bikini';
```

That's the only change needed on the app side.

## Updating the sample mapping later

If a sample's title in the app ever changes again (like the recent
Saliva/Swabs/Stool rename), update the `SAMPLE_FIELD_MAP` dictionary at the
top of `main.py` — nothing else needs to change. Since this is one plain text
file, `git diff` will show you exactly what changed, and there's no separate
"live" version to fall out of sync with, unlike the n8n node.

## Logs

Render's dashboard shows live logs (stdout) from the service, including a
line for every successful sync and every warning/error, so you can debug a
failed submission the same way we used n8n's Executions tab before.
