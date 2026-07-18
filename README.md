# Lead Generation Sample Delivery Pipeline

A Python automation pipeline that takes a raw customer inquiry, scrapes matching leads from Apollo.io and Apify, verifies their emails via ZeroBounce, and exports a quality-verified sample of 20–25 leads as a CSV.

---

## Pipeline Overview

```
Raw Inquiry Text
      │
      ▼
1. parse_inquiry()    ← Claude claude-sonnet-4-6 extracts structured ICP JSON
      │
      ▼
2. scrape_apollo()    ← Apollo.io People Search API
   scrape_apify()     ← Apify actor (LinkedIn / company scraper)
      │  (combined)
      ▼
3. dedupe_leads()     ← Deduplication + formatting standardization
      │
      ▼
4. verify_emails()    ← ZeroBounce batch validation
      │
      ▼
5. select_sample()    ← Best 20–25 leads by confidence + ICP match
      │
      ▼
6. export_csv()       ← leads_sample_TIMESTAMP.csv + report_TIMESTAMP.txt
```

---

## Requirements

- Python 3.11+
- API accounts for: **Anthropic**, **Apollo.io**, **Apify**, **ZeroBounce**

---

## Setup

### 1. Clone / copy the project

```bash
cd /path/to/icp
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure API keys

```bash
cp .env.example .env
```

Open `.env` and fill in your real API keys:

| Variable             | Where to get it |
|----------------------|-----------------|
| `ANTHROPIC_API_KEY`  | [console.anthropic.com](https://console.anthropic.com/) |
| `APOLLO_API_KEY`     | Apollo.io → Settings → Integrations → API Keys |
| `APIFY_API_TOKEN`    | [console.apify.com](https://console.apify.com/) → Settings → Integrations |
| `ZEROBOUNCE_API_KEY` | [app.zerobounce.net](https://app.zerobounce.net/) → API |
| `APIFY_ACTOR_ID`     | The actor slug from your Apify console, e.g. `apify/linkedin-profile-scraper` |

> **Never commit `.env` to version control.** It is already listed in `.gitignore`.

---

## Running the Pipeline

### Option A — Default test inquiry (hardcoded in `__main__`)

```bash
python pipeline.py
```

### Option B — Pass an inquiry as a CLI argument

```bash
python pipeline.py "I need CFOs at fintech startups in the UK with 10-50 employees"
```

### Option C — Import and call `main()` from your own script

```python
from pipeline import main

sample, csv_path, report = main(
    inquiry="Marketing directors at B2B SaaS companies in the US, 50-200 employees",
    output_dir="./output",
)
```

Output files are written to `./output/` by default (created automatically).

---

## Apify Actor Configuration

The Apify integration is intentionally left as a **placeholder** because different actors have different input schemas.

To configure it:

1. Find or build an actor in the [Apify Store](https://apify.com/store) (e.g. `bebity/linkedin-sales-navigator-scraper`).
2. Copy the actor's ID / slug into your `.env` as `APIFY_ACTOR_ID`.
3. Open `pipeline.py` and update `_build_apify_input()` to match that actor's input schema.
4. Update the field mapping inside `scrape_apify()` (the `.get("fullName")` etc. calls) to match that actor's output fields.

---

## Output Files

| File | Description |
|------|-------------|
| `output/leads_sample_YYYYMMDD_HHMMSS.csv` | Final sample — 20–25 verified leads |
| `output/report_YYYYMMDD_HHMMSS.txt` | Pipeline summary (counts, bounce rate) |

### CSV Columns

| Column | Description |
|--------|-------------|
| Name | Full name |
| Title | Job title |
| Company | Company name |
| Email | Verified email address |
| LinkedIn URL | Profile URL |
| Verification Status | `valid` / `invalid` / `catch-all` / … |
| Confidence Score | 0–100 (95+ required to pass) |
| Source | `apollo` or `apify` |

---

## Verification Threshold

Only leads with a **confidence score ≥ 95** are included in the final sample. This corresponds to ZeroBounce `valid` status with no problematic sub-statuses.

If fewer than 20 leads pass, a warning is printed to the console and written to the report.

---

## Error Handling

- Each stage catches API errors independently and logs them — a failure in one stage does not crash the pipeline.
- If an API key is missing, that stage is **skipped with a warning** (except Anthropic, which is required).
- Rate-limit pauses are built into the email verification batch loop.

---

## Project Structure

```
icp/
├── pipeline.py        ← Main pipeline (all stages)
├── requirements.txt   ← Python dependencies
├── .env.example       ← API key template (copy to .env)
├── .env               ← Your real keys (DO NOT COMMIT)
├── .gitignore         ← Excludes .env and output/
└── README.md          ← This file
```

---

## Adding to `.gitignore`

```
.env
output/
__pycache__/
.venv/
*.pyc
```
