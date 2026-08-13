# Eye Gaze Study — Django backend

Django backend for `Experiment2` (the static preview you uploaded). Ports
`index.html` / `js/*` into a real Django app and adds everything needed to
record and browse participant data, exactly like your Experiment 1 setup:
a Django admin you can check visually, with every response, every screen
transition/server hit, and both video streams tied to the participant and
timestamped.

## What's recorded

| Data | Model | Where |
|---|---|---|
| Every question's answer + Paas effort rating, with **presented-at** and **answered-at** timestamps | `QuestionResponse` | saved on each "Next" click on the rating screen |
| Every screen shown, every server hit, recording start/stop, fullscreen changes | `ActivityEvent` | logged continuously (see below) |
| Raw webcam + screen video, as uploaded MP4 chunks | `RecordingChunk` | one row per chunk upload |
| Final assembled webcam.mp4 / screen.mp4 per session | `Recording` | built automatically once the last chunk of a stream arrives, and again on session finish |
| Login, session start/end, IP, user agent | `StudySession` / `Participant` | one `StudySession` per login |

**Server hits** are logged automatically by `study/middleware.py` for
every `/api/...` call — you don't need to add anything for that. **Screen
shown** events are logged from `goTo()` in `app.js` on every screen
transition. **Recording started/stopped** events are logged from
`capture_session.js` at the exact moment `getUserMedia`/
`getDisplayMedia` resolve and when the streams are stopped — this is more
accurate than inferring "started" from chunk arrival, since chunk upload
lags behind stream acquisition by the encoder's first-segment buffering
time.

Open `/admin/`, click into a `StudySession`, and you'll see all of the
above inline — responses, events, and recordings — on one page, the same
visual-check workflow as before.

## Setup

```bash
python -m venv venv && source venv/bin/activate   # or your preferred env tool
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser   # for /admin/

python manage.py runserver
```

Open `http://127.0.0.1:8000/` for the study, `http://127.0.0.1:8000/admin/`
for the data.

### Config (environment variables, all optional)

| Variable | Default | Notes |
|---|---|---|
| `STUDY_PASSWORD` | `gaze2024` | shared password every participant enters |
| `STUDY_PARTICIPANT_COUNT` | `30` | accepted IDs are `"1"`..`"N"` |
| `REALEYE_STIMULUS_ID` | *(empty)* | your RealEye stimulus ID |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | dev defaults | set these for a real deployment |

## What changed vs. the static preview

- **`js/capture_session.js`**: uploads now go through `StudyAPI.uploadChunk`
  (CSRF header + `session_key` attached automatically) instead of a bare
  `fetch`. Explicit `webcam_recording_started/stopped` and
  `screen_recording_started/stopped` events are logged at stream
  acquisition/teardown.
- **`js/app.js`**: login now calls the server (`StudyAPI.login`) for the
  real ID/password check and gets back a `session_key`; every screen
  transition, fullscreen change, and question answer is sent to Django;
  `finishStudy()` now calls `/api/finish-session/` after the recording
  pipeline drains, which also triggers the final chunk→recording assembly.
- **New `js/api_client.js`**: small `fetch` wrapper (`window.StudyAPI`)
  handling CSRF and the five API calls above.
- **`js/study_schema.js` / `study_config.js` / `study_engine.js` /
  `css/styles.css`**: copied over unchanged — nothing about the
  module/section/question model or styling needed to change.
- `index.html` became `study/templates/study/index.html` (Django template
  tags for static files, CSRF cookie, and the RealEye/participant-list
  context that used to be hardcoded/TODO).

## Endpoints

All POST, JSON body unless noted:

- `POST /api/login/` — `{participant_id, password}` → `{session_key}`
- `POST /api/log-event/` — `{session_key, event_type, screen_name?, detail?, client_timestamp?}`
- `POST /api/submit-response/` — `{session_key, module_id, section_id, question_id, answer_value, effort_rating, presented_at, answered_at}`
- `POST /api/upload-chunk/` — multipart: `session_key, stream_source, sequence, is_last, video_chunk`
- `POST /api/finish-session/` — `{session_key, end_reason}`

## Known limitations / things to decide before deploying

- **Fragmented-MP4 concatenation**: `study/utils.py` assembles the final
  recording by concatenating chunk bytes in `sequence` order, which works
  because MP4Box produces standard fragmented MP4 (init segment first,
  then movie fragments) — no `ffmpeg` dependency needed. If you ever
  change the muxer/chunking scheme, re-check this still holds.
- **Abandoned sessions**: if a participant's browser closes without
  hitting "End Study" or the timeout firing, no `finish-session` call
  ever lands (the `pagehide` handler only stops local capture — a
  `sendBeacon`-based abandonment ping isn't wired up because
  `sendBeacon` can't carry the CSRF header this project uses everywhere
  else). Such sessions stay `end_reason="in_progress"` with `ended_at`
  empty — filter on that in the admin to spot them, or add a periodic
  sweep that marks stale in-progress sessions `abandoned`.
- **RealEye SDK wiring**: `initRealEye()` in `capture_session.js` is still
  a stub (`// Bind to RealEye life cycle calls if required...`), same as
  the original preview — fill in per your RealEye project's actual API if
  you need it to drive anything beyond screen-recording mode.
- **Media storage in production**: chunks + recordings save to
  `MEDIA_ROOT` (local disk) via Django's default file storage. Fine for
  local/dev; for a real deployment swap in `django-storages` (S3/GCS/etc.)
  by changing `DEFAULT_FILE_STORAGE` — no model changes needed.
- Every request currently uses SQLite for simplicity; swap `DATABASES` in
  `gaze_study/settings.py` for Postgres/MySQL for concurrent-participant
  production use.

## Tested

`python manage.py check` and a full local run (login → log-event →
submit-response → two chunk uploads → finish-session) were exercised
against this exact codebase — including confirming the two-chunk webcam
upload assembles into one file (`chunk_count=2`) and that the admin
session-detail page renders the inline response/event/recording lists.
Swap in your real question set via `study_config.js` /
`study_schema.js` (unchanged from the preview) before running it with
actual participants.
