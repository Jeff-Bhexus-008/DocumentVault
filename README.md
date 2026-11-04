# Doxari

A calm, secure home for your documents. Doxari is a Flask application on top of
Supabase Auth, Postgres and Storage. Upload, organise, preview, search, share and
recover documents, with every access decision made on the server.

## Requirements

- Python 3.10+
- A Supabase project with:
  - the existing `documents` table and private `document` storage bucket
  - the migration in `migrations/2026_11_04_doxari_upgrade.sql` applied once

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `SUPABASE_URL` | yes | Supabase project URL |
| `SUPABASE_KEY` | yes | Service-role key. Server-side only, never sent to the browser |
| `FLASK_SECRET_KEY` | yes | Session signing secret. Rotating it signs everyone out |
| `MAX_UPLOAD_MB` | no | Per-file upload cap, default `25` |
| `STORAGE_LIMIT_MB` | no | Enables "available storage" and threshold notices |
| `AI_PROVIDER` | no | `openai`, `groq`, `openrouter`, `together`, `deepseek` |
| `AI_API_KEY` | no | API key for the selected provider |
| `AI_MODEL`, `AI_BASE_URL` | no | Model or endpoint overrides |

Without the AI variables the assistant panel reports that it is not configured.
Natural-language search works regardless, because it is deterministic.

## Run locally

```bash
pip install -r requirements.txt
export SUPABASE_URL=... SUPABASE_KEY=... FLASK_SECRET_KEY=...
python app.py            # http://127.0.0.1:5000
```

Gunicorn runs `app:app`, unchanged from earlier deployments, so the existing
Render service configuration still applies.

## Database upgrade

Open the SQL editor in Supabase and run the contents of
`migrations/2026_11_04_doxari_upgrade.sql`. It is idempotent and additive: it
adds nullable columns to `documents`, creates `folders`, `document_shares`,
`share_links`, `activity_log` and `user_preferences`, adds indexes, and enables
owner-scoped row level security. The same file is downloadable in the app under
Settings, so you can run it without this repo.

Until it is applied the app still runs: folders, trash, sharing, activity and
analytics stay hidden while the classic library keeps working.

## Tests

```bash
./tools/dev_services.sh start   # mock Supabase + app on :5055
python3 tools/smoke_test.py     # functional flows
python3 tools/security_test.py  # access control, CSRF, upload validation
./tools/dev_services.sh stop
```

## More detail

`UPGRADE_REPORT.md` documents the design system, implemented features, database
changes, security posture, optional integrations and known limitations.
