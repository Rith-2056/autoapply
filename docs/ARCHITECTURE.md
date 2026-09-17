# AutoApplier — architecture audit and implementation plan

## 1. Current architecture (audit, Sep 2026)

| Concern | What exists | Verdict |
|---|---|---|
| Interface | Typer CLI (`cli.py`), rich tables, Streamlit read-only dashboard | Replace as primary UI; keep CLI for dev/debug |
| Job discovery | `listings.py`: clones SimplifyJobs repo, parses `listings.json`, README fallback, filters | **Reuse as-is** |
| Job representation | `Listing` dataclass (id, company, title, url, locations, category, terms…) | Reuse; persist into a `jobs` table |
| Browser automation | Playwright, `ats/` handlers (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, generic), `ats/base.py` enumerate→plan→apply with read-back verification | **Reuse unchanged** |
| Question detection / answers | `classify.py` (18 categories, policies), `planner.py` (profile rules → one batched LLM call → filled / draft / needs_user / n/a), `fields.py` rules, `llm.py` (Anthropic, JSON schema, company research) | **Reuse unchanged** |
| Human-in-the-loop | `runner.py` uses terminal `Prompt.ask`; `resolve.py` conversational voice loop (TTS reads every question) | Refactor: extract an `Interaction` interface; terminal stays as one implementation, web becomes the primary one |
| Voice | `voice.py`: pyttsx3/`say` TTS, sounddevice mic, faster-whisper STT, silence detection | Keep as server-side fallback STT; TTS narration removed from the default path |
| Persistence | `db.py`: single flat `applications` table + `runs` in SQLite | Replace with a normalised tracker schema; migrate existing rows |
| Email | none | Build (Gmail API, OAuth, read-only) |
| Auth | none (single local user) | Local-only app bound to 127.0.0.1; no login |
| Config / secrets | `config/profile.yaml`, `config/settings.yaml`, `.env` | Reuse; profile becomes editable from the UI |
| Logging | per-run log file + rich console | Extend with structured events stored in the tracker |
| Tests | 96 tests: parser, ATS detection, DB, fields, classify, planner, resolve | Keep; add tracker, email, voice-correction, API tests |

Smallest set of changes that gets to the target product:

1. A tracker schema (jobs, applications, events, questions, answers, email events, actions, notifications, sessions) with an explicit state machine, plus a migration from the flat table.
2. An `Interaction` abstraction so the runner can be driven either by the terminal or by a web session that blocks on user answers.
3. A FastAPI backend that owns AutoApply sessions in a worker thread, exposes REST + Server-Sent Events, and serves a static single-page frontend.
4. A dictation-style voice path: browser speech capture (Web Speech API when available, else MediaRecorder → server faster-whisper), then a fast context-aware correction step with a non-hallucination guard and per-span confidence.
5. Gmail integration: OAuth (read-only), poller, rule+LLM classification and extraction, multi-signal matching, deadline parsing, event/action/notification generation.

## 2. Proposed architecture

```
web/static (SPA)  ──HTTP/SSE──▶  web/app.py (FastAPI)
                                   ├─ web/orchestrator.py   AutoApply sessions (thread worker), WebInteraction
                                   │     └─ runner.py → ats/* (Playwright)  [unchanged automation]
                                   ├─ email/monitor.py       Gmail poll → classify → match → events/actions/notifications
                                   ├─ voice/correction.py    transcript clean-up with guard
                                   └─ tracker/store.py       SQLite (single file, WAL)
```

Everything runs locally on the user's machine (the browser must be visible for review), so there is one user and no login. The API binds to localhost only.

## 3. Database (SQLite, `data/autoapply.db`)

jobs, applications, application_events, questions, answers, email_events, actions, notifications, autoapply_sessions, profile (JSON), email_accounts (encrypted token). See `tracker/store.py`. Legacy `data/applications.db` rows are imported by `tracker/migrate.py` on first start.

## 4. Application state machine

`tracker/states.py`: DISCOVERED → READY_TO_APPLY → APPLYING → NEEDS_INPUT ↔ APPLYING → SUBMITTED → CONFIRMATION_RECEIVED → ASSESSMENT → INTERVIEW → FINAL_INTERVIEW → OFFER; REJECTED / WITHDRAWN / FAILED reachable from the appropriate stages; invalid transitions raise. Status is derived from events (`EventType` → target status) so history is the source of truth.

## 5. Frontend pages

Dashboard, Applications (table, filters, search), Application detail (timeline, Q&A, emails, actions), Action Center, AutoApply (config panel, live session, answer panel with mic), Profile, Settings (email connect/disconnect, voice, LLM), Debug (event log).

## 6. Backend services

REST endpoints listed in `web/app.py` (applications, events, questions/answers, autoapply start/pause/resume/cancel/status, actions, notifications, profile, email connect/disconnect/status/sync, voice transcribe/clean). SSE stream at `/api/events/stream`.

## 7. Voice pipeline

Mic → (browser Web Speech API streaming | MediaRecorder → `/api/voice/transcribe` faster-whisper) → `/api/voice/clean` (context: company, role, question, resume vocabulary, profile) → cleaned text + confidence + uncertain spans with suggestions → UI. Questions are never read aloud. Guard: the correction may only edit; if it adds content beyond a small budget the raw transcript is kept and flagged low-confidence. Any system-wide dictation tool (e.g. Wispr Flow) can type straight into the answer box; the clean-up step still applies.

## 8. Email integration

Gmail read-only OAuth (desktop flow), token encrypted with Fernet (`AUTOAPPLY_SECRET_KEY`). Poller every N minutes: candidate filter by query (`newer_than`, keywords), rule-based pre-classification, LLM extraction (company, role, category, platform, deadline, action), multi-signal matching with confidence (auto-link ≥ 0.8, ask 0.5–0.8, ignore < 0.5), event → status derivation, action + notification creation, deadline parsing to absolute timestamps.

## 9. Testing

State transitions; email classification and extraction (fixtures); matching (exact, company-only, similar roles, multiple apps per company, ambiguous); deadline parsing (absolute, relative, formats, missing); dedupe (URL, job id, cross-source, similar-but-different); voice correction (fixes homophones/terms, refuses additions); migration; API smoke tests with the FastAPI test client.

## 10. Migration

On first start of the web app: if `data/applications.db` exists and the tracker has no applications, import every row (job + application + one event + questions/answers) with status mapped from the legacy status, and keep the legacy file untouched.

## 11. Phases

1 audit (this document) · 2 tracker + migration · 3 web backend + SPA · 4 AutoApply integration (WebInteraction, sessions, live events) · 5 voice redesign · 6 email · 7 actions/deadlines/notifications · 8 reliability (retry/resume, error states) · 9 polish.

## 12. Additions (P0 67–72)

* **US-only gate** (`location.py`, `eligibility.py`): runs on listing data before any browser
  automation; verdict US / NON_US / MIXED / UNKNOWN; only US and MIXED-with-a-specific-US-office
  are eligible. Audit on the live SimplifyJobs data: 1115 US, 109 non-US, 6 mixed, 0 unknown.
* **Eligibility gate order**: us_location → excluded_location → configured_criteria
  (term, category, title keywords, exclusions, sponsorship, degree) → already_applied
  (tracker dedupe by URL / job id / company+title+location; retry re-admits FAILED /
  NEEDS_INPUT / WITHDRAWN) → ats_supported. The first failing check is the headline reason
  shown in the AutoApply preview.
* **Platform answer profiles** (`platforms.py`, `config/platforms.yaml`, tracker table
  `platform_questions`): per-ATS field overrides keyed by profile path and per-question
  answers; the planner consults them before profile rules and before the LLM; every question
  a platform asks is recorded so the Settings page is driven by real forms, not a hard-coded
  list. Answers confirmed in a session can be remembered per platform.
