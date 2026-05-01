# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

`tqdm` must be installed; `requests` ships with Ubuntu's Python:

```bash
sudo apt install python3-tqdm
```

There is no build step. Run directly with:

```bash
python3 scraper.py <command>
```

## Workflow

Typical usage is a two-phase pipeline:

```bash
python3 scraper.py sync          # phase 1: populate decisions.db with all slugs (~2 min)
python3 scraper.py fetch-all     # phase 2: fetch full content for every decision (hours, resumable)
python3 scraper.py stats         # inspect what's in the DB
python3 scraper.py export        # dump to decisions.jsonl
```

For incremental updates, re-running `sync` detects new/updated decisions by comparing `public_timestamp`; `fetch-all` only processes rows where `fetched_at IS NULL`.

## Architecture

Everything is in `scraper.py`. The data flow is:

1. **GOV.UK Search API** (`/api/search.json`) — paginated list of all decisions, max 1000/page, sorted by `-public_timestamp`. Provides: slug, title, published timestamp.
2. **GOV.UK Content API** (`/api/content{slug}`) — full record for one decision. The full decision text lives in `details.metadata.hidden_indexable_content` (already extracted from the PDF — no PDF download needed). Other useful fields: `details.metadata.tribunal_decision_decision_date`, `tribunal_decision_country`, `tribunal_decision_categories`; `details.attachments[].url` for the PDF if needed.
3. **SQLite DB** (`decisions.db`, WAL mode) — single `decisions` table. `slug` is the primary key. `fetched_at IS NULL` means the row has been indexed but full content not yet retrieved.

The `fetch-all` command is intentionally safe to interrupt: each row is committed as soon as it's written, so Ctrl+C and re-running resumes from where it left off.

Threading in `fetch-all`: worker threads do HTTP only; all DB writes go through a `threading.Lock` to serialise SQLite access.

## DB Schema

```sql
decisions (
    slug            TEXT PRIMARY KEY,   -- e.g. /employment-tribunal-decisions/smith-v-acme-1234-slash-2024
    title           TEXT,
    published_at    TEXT,               -- ISO8601, from search API
    indexed_at      TEXT NOT NULL,      -- when this row was first created locally
    fetched_at      TEXT,               -- NULL = content not yet fetched
    decision_date   TEXT,               -- YYYY-MM-DD, the actual hearing/judgment date
    country         TEXT,               -- "england-and-wales" | "scotland"
    categories      TEXT,               -- JSON array e.g. ["unfair-dismissal", "race-discrimination"]
    full_text       TEXT,               -- extracted decision text (~10–100k chars)
    pdf_url         TEXT,               -- direct URL to PDF on assets.publishing.service.gov.uk
    content_id      TEXT                -- GOV.UK content ID (UUID)
)
```
