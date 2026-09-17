# AutoApplier

Your job-search command center for **Summer 2027 internships**: discovers jobs from the
[SimplifyJobs list](https://github.com/SimplifyJobs/Summer2027-Internships), applies with a
visible browser, asks you only what it cannot answer (by dictation or typing), tracks every
application, reads your Gmail for assessments / interviews / rejections, and tells you what to
do next.

```bash
autoapply web          # opens http://127.0.0.1:8710
```

**Web app** (primary interface): Dashboard · Applications · Action Center · AutoApply · Profile ·
Settings · Debug. The terminal commands still exist for development (`autoapply run`,
`status`, `export`, `listings`, `check`).

Architecture and the implementation plan: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How it works

```
Discover (SimplifyJobs listings.json) → filter → open in Playwright → fill from profile
→ classify every question → answer / draft / ask you → you answer (mic or keyboard)
→ contextual clean-up of what you said → review → submit → tracker
→ Gmail monitor: classify emails → match to application → status / actions / deadlines
→ Action Center + notifications
```

* Automation, question classification and inference are unchanged from the CLI version
  (`ats/`, `classify.py`, `planner.py`, `llm.py`). The web app drives them through an
  `Interaction` interface (`interaction.py`, `web/orchestrator.py`).
* Every application has an explicit **state machine** (`tracker/states.py`) and an
  **event log**; status is derived from events, invalid transitions are rejected.
* **Voice is dictation, not a conversation.** Questions are displayed, never read aloud.
  Press 🎙, speak, stop; the transcript appears immediately (browser speech recognition,
  streaming) and is cleaned with context (company, role, question, your resume vocabulary).
  Uncertain words are highlighted with "Did you mean …?" chips. The clean-up may only edit
  what you said: a guard rejects any result that adds content. Any system-wide dictation
  tool (e.g. Wispr Flow) also works: dictate into the answer box, then press "Clean up".
* **US jobs only (hard rule).** Every listing passes an eligibility gate *before* the browser
  opens: US location → target role → configured criteria → not a duplicate / already applied
  → supported platform. The location validator understands "Boston, MA", state names,
  well-known US cities, "Remote - US", "Remote in USA", territories, and rejects other
  countries, Canadian provinces, country-level mixes ("United States / Canada", "US / UK")
  and bare "Remote" (unknown → never auto-applied). Several specific US offices listed next
  to international ones count as US. The AutoApply preview shows every skipped listing with
  its reason. `filters.us_only` in settings.yaml is `true`; it is only ever relaxed explicitly.
* **Platform answer profiles.** Settings → Application platforms has a tab per ATS (Workday,
  Greenhouse, Lever, Ashby, SmartRecruiters, Other). Each shows the canonical fields with the
  profile default and a platform-only override, plus every question that platform has
  actually asked (recorded from real applications) with an answer box. Configured answers win
  over `profile.yaml` on that platform and never go to the LLM. Confirming an answer during a
  session with "Remember for this platform" checked stores it (`config/platforms.yaml`).
  Workday accounts stay in `.env` (`WORKDAY_ACCOUNTS`).
* **Email intelligence** (Gmail, read-only): rule + LLM classification, structured
  extraction (company, role, platform, deadline, action), multi-signal matching with
  confidence (auto-link ≥ 0.8, ask you 0.5–0.8, ignore below), deadline parsing to real
  timestamps, automatic status updates, actions and notifications.

---

## 1. Setup

Requirements: Python 3.11+, git, and [uv](https://docs.astral.sh/uv/) (or plain pip).

```bash
git clone <this repo> autoapply && cd autoapply

# with uv
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# browser for Playwright (one time)
playwright install chromium

# optional: voice answering (microphone + local speech-to-text + text-to-speech)
uv pip install -e ".[voice]"      # or: pip install -e ".[voice]"
```

Then:

1. **Resume** – put your PDF at `resume/Divyarith_Resume.pdf` (path is configurable in
   `config/profile.yaml`) and extract its text for the LLM:
   ```bash
   autoapply extract-resume        # writes resume/resume.txt
   ```
2. **Profile** – open `config/profile.yaml` and replace every `[FILL IN]`
   (address / city, US work authorization, sponsorship, relocation, citizenship,
   transgender status, pronouns). Anything left as `[FILL IN]` is never typed into a
   form; the question is asked of you instead. Education dates (August 2024 → May 2028),
   gender, race/ethnicity and sexual orientation are already filled in.
3. **Secrets** – `cp .env.example .env` and set:
   * `ANTHROPIC_API_KEY` – used for inference, drafts and company research. Without it
     every question the profile rules can't answer is handed to you.
   * `WORKDAY_ACCOUNTS` – optional JSON mapping a Workday hostname to
     `{"email": ..., "password": ...}`. Workday listings without credentials are marked
     `needs_manual`.
4. **Check** everything, then start the web app:
   ```bash
   autoapply check
   autoapply web
   ```
   The old flat database (`data/applications.db`) is imported into the tracker
   (`data/autoapply.db`) on first start.

5. **Email (optional but recommended)** – in Google Cloud Console create a project, enable
   the **Gmail API**, create an **OAuth client ID of type Desktop app**, download the JSON
   and save it as `config/google_oauth_client.json` (git-ignored; or set
   `GOOGLE_OAUTH_CLIENT_FILE`). Then Settings → **Connect Gmail**: a Google sign-in opens,
   requesting the read-only scope only. The token is stored encrypted (Fernet; key from
   `AUTOAPPLY_SECRET_KEY` or an auto-generated `data/.secret_key`). Disconnect deletes it.
   Only job-related emails are processed; irrelevant mail is never stored.

`.env`, `data/`, `resume/` and `logs/` are git-ignored.

---

## 2. Configuration (`config/settings.yaml`)

```yaml
listings:
  repo_url: https://github.com/SimplifyJobs/Summer2027-Internships
  branch: dev
  local_path: ./data/listings_repo      # cloned on first run, `git pull` on every run
  json_path: .github/scripts/listings.json
  term: "Summer 2027"

filters:
  categories: [Software, "AI/ML/Data", Quant]   # also: Hardware, Product
  title_keywords: []                 # title must contain one of these (empty = any)
  exclude_title_keywords: ["PhD", "MBA", "Master's", "Masters", "Graduate Student"]
  locations: []                      # substrings, e.g. ["Boston", "New York", "Remote"]
  exclude_companies: []
  exclude_sponsorship: ["U.S. Citizenship is Required"]
  require_degree: "Bachelor's"       # listing's degree list must include this (if it has one)
  ats_allowlist: [greenhouse, lever, ashby, smartrecruiters, workday, generic]
  max_applications_per_run: 10       # --limit overrides

run:
  headless: false                    # visible browser by default
  chromium_executable: ""            # optional path to an existing Chrome/Chromium
  delay_min_seconds: 20              # random pause between applications
  delay_max_seconds: 60
  page_timeout_ms: 45000
  screenshots_dir: ./data/screenshots
  browser_profile_dir: ./data/browser_profile   # cookies persist between runs
  database: ./data/applications.db
  logs_dir: ./logs

llm:
  model: claude-opus-5
  min_confidence: medium             # medium = reasonable inference allowed; high = explicit facts only
  max_resume_chars: 12000
  web_research: true                 # web-search the company/role before drafting "why us" answers
  research_cache_dir: ./data/research
  max_job_text_chars: 8000

voice:                               # used by `autoapply run --voice`
  tts: auto                          # auto | pyttsx3 | say (macOS) | off
  stt: faster_whisper                # faster_whisper | off
  whisper_model: base.en             # downloaded on first use (~150 MB); try small.en for accuracy
  language: en
  max_record_seconds: 90
  silence_seconds: 2.5
  sample_rate: 16000
```

Category aliases such as `software`, `ml`, `data science`, `quant` are normalised to the
values used in `listings.json` (`Software`, `AI/ML/Data`, `Quant`, `Hardware`, `Product`).

### Listing data format

`listings.json` is a list of objects like:

```json
{
  "id": "2e987576-...", "source": "Simplify", "company_name": "GDIT",
  "title": "Software Development Intern", "category": "Software",
  "url": "https://gdit.wd5.myworkdayjobs.com/.../RQ228405",
  "locations": ["Falls Church, VA"], "terms": ["Summer 2027"],
  "sponsorship": "Other", "degrees": ["Bachelor's", "Master's"],
  "date_posted": 1789430400, "date_updated": 1789482582,
  "active": true, "is_visible": true, "company_url": "https://simplify.jobs/c/GDIT"
}
```

A listing is considered open when `active` and `is_visible` are true and `terms`
contains the configured term. This is the same logic the repo's own README generator uses.

---

## 3. Web app

| Page | What it does |
|---|---|
| Dashboard | Counts (total / today / week / month / need action / assessments / interviews / offers), action-required list, current AutoApply session, recent applications. Live via Server-Sent Events. |
| Applications | Tracker table: company, position, applied date, status, next action. Filters: status, date range, needs-action; search box understands "needs action", "assessment", "interview", company names, locations. Rows open the detail page. |
| Application detail | Status (with manual override, invalid transitions are refused), next action + deadline, application facts, timeline, every question with raw transcription / cleaned / final answer / source / approval, linked emails, job description, Retry/resume for failed or needs-input applications. |
| Action Center | "What do I need to do right now?": open actions sorted by priority and deadline (Open / View / Done / dismiss), applications waiting for a response, recently completed. |
| AutoApply | Configure a session (target roles, locations, max applications, categories, exclusions, dry run, retry) with a preview of matching jobs. Live session: progress, per-application steps, question cards with 🎙 Speak / ✨ Clean up / Reuse answer / Skip / Confirm, review panel (Submit / I edited it in the browser / Skip / Leave for later), CAPTCHA pause (I solved it / Skip). Pause / Resume / Cancel. The session runs in the backend; refreshing the page does not stop it. |
| Profile | Edit `profile.yaml` sections; placeholders are highlighted. Resume text preview. |
| Settings | Gmail connect / sync / disconnect, possible-match confirmations, AI key status, voice clean-up toggle, filter defaults. |
| Debug | Session state, recent internal events, email monitor state. |

### Answering questions (voice)

1. The question card shows the label, category, why it needs you, and options if any.
2. Press **🎙 Speak**. Chrome/Edge/Safari transcribe in the browser as you talk (interim
   text streams into the box). Other browsers record and send the audio to the server,
   which transcribes with faster-whisper if the voice extras are installed.
3. When you stop, the transcript is cleaned with context and shown with a confidence
   level. Uncertain words are highlighted with suggestion chips; nothing is ever added.
4. Edit if you like, then **Confirm answer**. The answer goes into *that* form field
   (mapped by field id) and is verified. Long answers you confirm become reusable
   ("Reuse answer" suggests them on similar questions later; you still click to use them).
5. **Continue to review** → the review panel lists every field. **Submit** is disabled
   while required fields are blank or drafts are unapproved. Nothing submits without your click.

## 3b. CLI commands (development / debugging)

| Command | What it does |
|---|---|
| `autoapply web` | Start the web app (`--port`, `--host`, `--no-browser`). |
| `autoapply run` | Sync listings, filter, apply. **Review mode by default.** |
| `autoapply run --dry-run` | Fill forms, never submit. Records status `dry_run`. |
| `autoapply run --auto` | Submit without confirmation. |
| `autoapply run --limit 3` | Cap the number of applications this run. |
| `autoapply run --ats greenhouse,lever` | Only attempt these ATS types. |
| `autoapply run --company doordash` | Only listings whose company matches. |
| `autoapply run --retry` | Re-attempt listings previously `failed` / `needs_manual` / `skipped`. |
| `autoapply run --voice` | Read each unresolved question aloud and answer by microphone (see §5). |
| `autoapply run --headless` / `--headed` | Override `run.headless`. |
| `autoapply run --no-sync` | Skip the `git pull`. |
| `autoapply listings` | Show listings matching your filters, with detected ATS and DB status. |
| `autoapply status` | Table of tracked applications (`--status SUBMITTED`, `--company`, `--since`, `--until`, `--search`, `--needs-action`, `--limit`). |
| `autoapply export --out data/applications.csv` | Export the tracker to CSV. |
| `autoapply dashboard` | Legacy Streamlit view (prefer `autoapply web`). |
| `autoapply check` | Validate profile placeholders, resume, API key, browser. |
| `autoapply extract-resume` | Regenerate `resume/resume.txt` from the PDF. |

### Review mode

For each listing the browser fills the form. Questions that need you are then walked
through one by one (voice or typed, see §5). Finally the terminal prints a table of every
field with its value, source (`profile`, `resume`, `inferred`, `draft`, `user`) and status
(`filled`, `needs approval`, `NEEDS YOU`, `n/a`). You choose:

* `y` – submit
* `n` – skip (status `skipped`; or `needs_manual` if required questions or unapproved drafts remain)
* `e` – edit the form yourself in the browser, press Enter, then confirm submission

`y` is refused while a required question is blank or a draft is unapproved. A spoken or
typed answer never submits anything by itself.

If a CAPTCHA appears, review mode pauses so you can solve it in the browser.

### Application states

`DISCOVERED → READY_TO_APPLY → APPLYING ⇄ NEEDS_INPUT → SUBMITTED → CONFIRMATION_RECEIVED →
ASSESSMENT → INTERVIEW → FINAL_INTERVIEW → OFFER`, plus `REJECTED`, `WITHDRAWN`, `FAILED`,
`UNKNOWN`. Transitions are explicit (`tracker/states.py`); a late confirmation email never
moves an application backwards and a rejected one is not revived by an old email. Jobs are
de-duplicated by normalised URL, external job id, and company + title + location, so the
same posting seen on two boards is one job. Failed / needs-input applications can be
retried from the detail page; the run resumes on the same application record.

---

## 4. How answers are decided

Every form field is classified (`src/autoapply/classify.py`) into a category, and each
category has a policy:

| Category | Policy | What happens |
|---|---|---|
| personal info, contact | profile | Filled from `profile.yaml` rules. No rule / no value → asked. Never inferred. |
| education | infer | Rules first (school, degree, major, GPA incl. ranges, start Aug 2024, end May 2028, "Spring 2028"-style graduation selects); otherwise the LLM may infer from the resume (e.g. "currently enrolled?", "received an academic honor?" → Dean's List / Adams Scholarship). |
| employment, technical skills | infer | Inferred from the resume ("completed at least one internship?", "years of experience", "technical domains you're interested in" → the domains that recur in your projects). |
| work authorization, sponsorship | strict | Explicit profile facts only ("authorized in the US", "requires sponsorship"). OPT/CPT/clearance questions the profile doesn't state are asked. Never inferred for another country. |
| demographics | profile | Gender, race/ethnicity, sexual orientation, Hispanic/Latino, veteran, disability from the profile. Transgender status / pronouns are `[FILL IN]` → asked. The LLM never sees these. |
| availability | infer | Start date / term / willingness to work on-site from the profile, else inferred. |
| salary | ask | Always you. |
| job source ("How did you hear about us?") | blank | Left blank and asked. Never inferred. |
| company motivation, role motivation, behavioral | draft | The LLM writes a first-person draft grounded in your real projects and the company/role research, filled into the form and flagged **needs approval**. You accept, edit, re-answer by voice, or skip. |
| short answer, long form, other | infer | The LLM decides per question: answer (fact or reasonable inference), draft (subjective), or ask you. |

The decision engine (`src/autoapply/planner.py`) runs profile rules first, then makes
**one batched LLM call** per form for everything left (`src/autoapply/llm.py`). Each LLM
result is `answer` / `draft` / `ask_user` with a confidence and a one-line reason;
answers below `llm.min_confidence` become `ask_user`, and answers to option fields must
match an option exactly.

The LLM is given your resume text, the profile facts, the job posting text captured from
the page, and (when `llm.web_research` is on) a cached research brief about the company
and role produced with web search (`data/research/<company>-<role>.md`). It is instructed
never to invent employers, titles, dates, degrees, GPA, projects, technologies, awards,
demographics, motivations, or how you found the job.

After every value is typed it is read back; a value that did not stick is reported as
needing you rather than assumed. The result is that every field ends in exactly one bucket:
`filled`, `needs approval` (draft), `NEEDS YOU`, or `n/a` (cover-letter upload, password).

## 5. CLI voice answering (legacy)

```bash
autoapply run --voice            # review mode + voice
autoapply run --voice --dry-run  # try the whole flow without submitting
```

After the form is filled, each question that needs you is handled in order (required
first):

1. The question (and its options, if any) is printed and **read aloud**.
2. **● Listening…** – your microphone records until you pause for `silence_seconds`
   (or `max_record_seconds`). Silence for a few seconds ends the attempt gracefully.
3. The recording is transcribed locally with faster-whisper and shown to you.
4. You choose: `a` accept · `r` retry · `e` edit the text · `t` type instead · `s` skip.
   For option fields a spoken answer is matched to an option ("yes", "option three", or
   the option's text); if nothing matches you can pick by number.
5. On accept, the answer is written into **that field only** (mapped by the field's id,
   never by position) and verified. Then the next question is read.
6. Drafts are read out as "I drafted an answer for …" and shown; accept, edit, answer by
   voice instead, or skip (which clears the draft from the form).

When the run finishes the questions, the normal review table is shown and you still have
to type `y` to submit. Voice input never submits an application on its own.

If the voice packages aren't installed or the microphone isn't available, the same flow
runs with typed answers. Text-to-speech uses macOS `say` when available, else pyttsx3.

## 6. ATS handlers

| ATS | Detection | Notes |
|---|---|---|
| Greenhouse | `*.greenhouse.io`, `gh_jid=` | New job-boards and classic boards; embedded iframes on company sites. |
| Lever | `jobs.lever.co/...` | Goes straight to `/apply`. Lever often shows an hCaptcha on submit → review mode pauses, auto mode marks `needs_manual`. |
| Ashby | `jobs.ashbyhq.com` | Uses the `/application` page. |
| SmartRecruiters | `jobs.smartrecruiters.com` | Clicks "I'm interested" then fills. |
| Workday | `*.myworkdayjobs.com` | Requires `WORKDAY_ACCOUNTS`; otherwise `needs_manual`. With credentials: sign in, "Apply Manually", fill page by page. Best-effort. |
| generic | everything else | Looks for an Apply button and a form with a resume upload; otherwise `needs_manual`. |

Links that redirect (e.g. Simplify short links) are re-detected after navigation.

---

## 7. Project layout

```
config/       profile.yaml, settings.yaml
data/         applications.db, screenshots/, listings_repo/, browser_profile/  (git-ignored)
resume/       Divyarith_Resume.pdf, resume.txt                                (git-ignored)
logs/         run_<id>.log per run                                            (git-ignored)
src/autoapply/
  cli.py          Typer CLI
  runner.py       run orchestration, review prompt, captcha gate, delays
  listings.py     repo sync, listings.json parser, README fallback, filters
  ats/            detection + one handler per ATS (base.py: enumerate → plan → apply)
  classify.py     question categories and per-category policies
  planner.py      decision engine: profile rules, then one batched LLM call
  fields.py       label → profile mapping rules and option matching
  llm.py          Anthropic-backed inference (answer / draft / ask_user) + company research
  interaction.py  how the runner asks a human (terminal implementation)
  location.py     US location validation (states, cities, remote-US, non-US countries/provinces)
  eligibility.py  the pre-browser eligibility gate with per-check reasons
  platforms.py    per-ATS answer profiles (config/platforms.yaml) and the canonical field catalogue
  tracker/        state machine, SQLite store, dedupe, legacy migration
  web/            FastAPI app, AutoApply session orchestrator, SSE bus, static SPA
  dictation.py    transcript clean-up with vocabulary correction and a no-invention guard
  mail/           Gmail OAuth/fetch, classification + extraction, matching, deadlines, monitor
  resolve.py      CLI conversational flow (legacy --voice)
  voice.py        CLI text-to-speech / microphone / whisper backends
  db.py           SQLite layer
  report.py       terminal tables
  dashboard.py    Streamlit app
tests/            listings, ATS detection, fields, classification, planner (fake LLM),
                  tracker state machine / store / migration / dedupe, email classification /
                  matching / deadlines / processing, dictation guard, API smoke tests
```

Run the tests:

```bash
pytest
```

---

## 8. Troubleshooting

* **`Executable doesn't exist`** – run `playwright install chromium`, or set
  `run.chromium_executable` (or env `AUTOAPPLY_CHROMIUM`) to an existing Chrome binary.
* **Behind a TLS-intercepting corporate proxy** – set `AUTOAPPLY_IGNORE_TLS_ERRORS=1`
  (only then; it disables certificate checks in the automation browser).
* **Dropdowns not selected** – custom dropdowns (react-select etc.) are opened, typed
  into, and verified. If verification fails the field is reported as unanswered; use
  review mode's `e` to pick it yourself.
* **Nothing matches** – `autoapply listings` shows what passes your filters and why
  candidates are excluded (already in DB, ATS not allowed).
* **Mic button does nothing / "network" error** – browser speech recognition needs
  Chrome, Edge or Safari with microphone permission for `http://127.0.0.1`. Firefox falls
  back to server transcription, which needs the voice extras (`pip install -e ".[voice]"`).
* **Gmail "client file missing"** – create the Desktop OAuth client (Setup step 5).
* **Emails matched to the wrong application** – low-confidence matches are never
  auto-linked; confirm or ignore them under Settings → Email. Wrong auto-links can be
  changed on the application's Emails tab via the API (`POST /api/email/events/{id}/link`).
* **Too many questions asked** – add the fact to `profile.yaml` (e.g. `citizenship`,
  `transgender`, `how_did_you_hear`) and it will be filled next time.
