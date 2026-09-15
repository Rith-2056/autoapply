# autoapply

A Python CLI that applies to **Summer 2027 internships** from the
[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)
list with a real (headed) browser, and records every attempt so you can always see
what it applied to.

* Listings come from the repo's structured `.github/scripts/listings.json`
  (README table parsing is only a fallback).
* One handler per applicant tracking system (ATS): Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday (needs per-company credentials), and a generic fallback.
* Standard fields are filled from `config/profile.yaml`; the resume PDF is uploaded.
* Free-text / unexpected questions go to an LLM that is given only your resume text and
  profile and must refuse rather than invent anything. Low-confidence or unanswerable
  questions stop the application and mark it `needs_manual` with the question text saved.
* CAPTCHAs are never bypassed. In review mode you solve them in the browser; in auto
  mode the job is marked `needs_manual`.
* Every attempt is stored in SQLite (`data/applications.db`) with a screenshot.
  `autoapply status`, `autoapply dashboard` (Streamlit) and `autoapply export` show it.

> **Read before running for real.** `--review` is the default: the tool fills the form,
> shows you every answer, and only submits when you type `y`. Use `--dry-run` for the
> first few runs. `--auto` submits without asking.

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
```

Then:

1. **Resume** – put your PDF at `resume/Divyarith_Resume.pdf` (path is configurable in
   `config/profile.yaml`) and extract its text for the LLM:
   ```bash
   autoapply extract-resume        # writes resume/resume.txt
   ```
2. **Profile** – open `config/profile.yaml` and replace every `[FILL IN]`
   (address / city, US work authorization, sponsorship, relocation, race/ethnicity,
   Hispanic/Latino). Anything left as `[FILL IN]` is never typed into a form; if a form
   requires it, that job becomes `needs_manual`.
3. **Secrets** – `cp .env.example .env` and set:
   * `ANTHROPIC_API_KEY` – used for unexpected questions. Without it those questions are
     left blank and the job is marked `needs_manual`.
   * `WORKDAY_ACCOUNTS` – optional JSON mapping a Workday hostname to
     `{"email": ..., "password": ...}`. Workday listings without credentials are marked
     `needs_manual`.
4. **Check** everything:
   ```bash
   autoapply check
   ```

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
  min_confidence: high               # low | medium | high — answers below this are rejected
  max_resume_chars: 12000
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

## 3. Commands

| Command | What it does |
|---|---|
| `autoapply run` | Sync listings, filter, apply. **Review mode by default.** |
| `autoapply run --dry-run` | Fill forms, never submit. Records status `dry_run`. |
| `autoapply run --auto` | Submit without confirmation. |
| `autoapply run --limit 3` | Cap the number of applications this run. |
| `autoapply run --ats greenhouse,lever` | Only attempt these ATS types. |
| `autoapply run --company doordash` | Only listings whose company matches. |
| `autoapply run --retry` | Re-attempt listings previously `failed` / `needs_manual` / `skipped`. |
| `autoapply run --headless` / `--headed` | Override `run.headless`. |
| `autoapply run --no-sync` | Skip the `git pull`. |
| `autoapply listings` | Show listings matching your filters, with detected ATS and DB status. |
| `autoapply status` | Table of all recorded applications. Filters: `--status`, `--company`, `--since`, `--until`, `--run`, `--search`, `--limit`. |
| `autoapply dashboard` | Local Streamlit dashboard (counts, search/filter, job links, screenshots, CSV download). |
| `autoapply export --out data/applications.csv` | Export to CSV (same filters as `status`). |
| `autoapply check` | Validate profile placeholders, resume, API key, browser. |
| `autoapply extract-resume` | Regenerate `resume/resume.txt` from the PDF. |

### Review mode

For each listing the browser fills the form, then the terminal prints a table of every
field, the value, and its source (`profile`, `resume`, `llm`, `skipped`). You choose:

* `y` – submit
* `n` – skip (status `skipped`; or `needs_manual` if required questions were blank)
* `e` – edit the form yourself in the browser, press Enter, then confirm submission

If a CAPTCHA appears, review mode pauses so you can solve it in the browser.

### Statuses

| Status | Meaning |
|---|---|
| `applied` | Submitted and a confirmation page was detected (screenshot saved). |
| `failed` | Navigation/handler error or no confirmation after submit (screenshot saved). |
| `needs_manual` | Stopped on purpose: unanswerable required question, CAPTCHA in auto mode, Workday without credentials, unknown ATS without a form, or you chose to finish it yourself. Unanswered questions are stored. |
| `skipped` | You declined in review mode. |
| `dry_run` | Filled but never submitted. Dry-run records don't stop the listing from being attempted later. |

Listings that already have any record other than `dry_run` are skipped on later runs
(use `--retry` to reconsider `failed` / `needs_manual` / `skipped`).

---

## 4. How answers are decided

1. **Profile rules** (`src/autoapply/fields.py`): the field label is matched against
   patterns (name, email, phone, LinkedIn, GitHub, school, degree, GPA, graduation,
   address, work authorization, sponsorship, relocation, EEO questions, "how did you hear",
   consent/acknowledgement, SMS opt-in, …) and the matching value from `profile.yaml`
   is typed or selected. Dropdown options are matched fuzzily (Yes/No semantics, GPA
   ranges, month names). Identity rules only match short labels, so a long question that
   merely mentions "email" is not filled with your email address.
2. **Resume upload** for any `resume`/`CV` file input. Cover-letter uploads are skipped.
3. **LLM** (only for unknown *required* questions, or unknown free-text questions):
   the model sees your resume text + profile and returns `{can_answer, answer, confidence}`.
   It is instructed never to invent facts; when given options it must return one of them
   verbatim. Only answers at or above `llm.min_confidence` are used.
4. Anything else stays blank. If it was required, the job becomes `needs_manual` and
   the question text is saved in the database.
5. After typing, every text field is read back; a value that did not stick is reported
   as unanswered rather than assumed.

Every profile value that is still `[FILL IN]` is treated as unknown.

---

## 5. ATS handlers

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

## 6. Project layout

```
config/       profile.yaml, settings.yaml
data/         applications.db, screenshots/, listings_repo/, browser_profile/  (git-ignored)
resume/       Divyarith_Resume.pdf, resume.txt                                (git-ignored)
logs/         run_<id>.log per run                                            (git-ignored)
src/autoapply/
  cli.py          Typer CLI
  runner.py       run orchestration, review prompt, captcha gate, delays
  listings.py     repo sync, listings.json parser, README fallback, filters
  ats/            detection + one handler per ATS (base.py has the generic filler)
  fields.py       label → profile mapping rules and option matching
  llm.py          Anthropic-backed question answering with refusal
  db.py           SQLite layer
  report.py       terminal tables
  dashboard.py    Streamlit app
tests/            parser, ATS detection, database, field-mapping tests
```

Run the tests:

```bash
pytest
```

---

## 7. Troubleshooting

* **`Executable doesn't exist`** – run `playwright install chromium`, or set
  `run.chromium_executable` (or env `AUTOAPPLY_CHROMIUM`) to an existing Chrome binary.
* **Behind a TLS-intercepting corporate proxy** – set `AUTOAPPLY_IGNORE_TLS_ERRORS=1`
  (only then; it disables certificate checks in the automation browser).
* **Dropdowns not selected** – custom dropdowns (react-select etc.) are opened, typed
  into, and verified. If verification fails the field is reported as unanswered; use
  review mode's `e` to pick it yourself.
* **Nothing matches** – `autoapply listings` shows what passes your filters and why
  candidates are excluded (already in DB, ATS not allowed).
