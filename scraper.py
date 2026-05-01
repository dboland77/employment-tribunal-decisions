#!/usr/bin/env python3
"""
Employment Tribunal Decisions Scraper

Commands:
  sync          Fetch all decision slugs from GOV.UK into the local DB index
  fetch-all     Fetch full content (text, metadata) for every indexed decision
  fetch <slug>  Fetch full content for a single decision
  stats         Show database statistics
  export        Export DB to newline-delimited JSON (decisions.jsonl)
"""
import argparse
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from tqdm import tqdm

BASE_URL = "https://www.gov.uk"
SEARCH_API = f"{BASE_URL}/api/search.json"
CONTENT_API = f"{BASE_URL}/api/content"
PAGE_SIZE = 1000
DB_PATH = Path("decisions.db")

_session = requests.Session()
_session.headers["User-Agent"] = "ET-decisions-scraper/1.0 (research; dboland77@gmail.com)"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decisions (
            slug            TEXT PRIMARY KEY,
            title           TEXT,
            published_at    TEXT,
            indexed_at      TEXT NOT NULL,
            fetched_at      TEXT,
            decision_date   TEXT,
            country         TEXT,
            categories      TEXT,   -- JSON array e.g. ["unfair-dismissal"]
            full_text       TEXT,
            pdf_url         TEXT,
            content_id      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_published  ON decisions(published_at);
        CREATE INDEX IF NOT EXISTS idx_decision_date ON decisions(decision_date);
        CREATE INDEX IF NOT EXISTS idx_country     ON decisions(country);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# GOV.UK API helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            resp = _session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def search_page(start: int) -> dict:
    return _get(SEARCH_API, {
        "filter_content_store_document_type": "employment_tribunal_decision",
        "count": PAGE_SIZE,
        "start": start,
        "fields": "title,link,public_timestamp",
        "order": "-public_timestamp",
    })


def content_for_slug(slug: str) -> dict | None:
    """Fetch full content from the GOV.UK Content API. Returns None if 404."""
    path = slug if slug.startswith("/") else f"/employment-tribunal-decisions/{slug}"
    return _get(f"{CONTENT_API}{path}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_sync(args):
    """Walk every search page and upsert slugs into the local index."""
    conn = open_db()
    now = datetime.now(timezone.utc).isoformat()

    first = search_page(0)
    total = first["total"]
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"Remote total: {total:,} decisions across {pages} pages")

    inserted = updated = skipped = 0

    with tqdm(total=total, desc="Syncing index", unit="decisions") as bar:
        for page_num in range(pages):
            data = search_page(page_num * PAGE_SIZE)
            results = data["results"]

            rows_to_insert = []
            rows_to_update = []

            for r in results:
                slug = r["link"]
                title = r.get("title", "")
                published_at = r.get("public_timestamp", "")

                existing = conn.execute(
                    "SELECT published_at FROM decisions WHERE slug=?", (slug,)
                ).fetchone()

                if existing is None:
                    rows_to_insert.append((slug, title, published_at, now))
                    inserted += 1
                elif existing["published_at"] != published_at:
                    # Decision was updated — clear fetched_at so content gets re-fetched
                    rows_to_update.append((title, published_at, slug))
                    updated += 1
                else:
                    skipped += 1

            if rows_to_insert:
                conn.executemany(
                    "INSERT OR IGNORE INTO decisions (slug, title, published_at, indexed_at) VALUES (?,?,?,?)",
                    rows_to_insert,
                )
            if rows_to_update:
                conn.executemany(
                    "UPDATE decisions SET title=?, published_at=?, fetched_at=NULL WHERE slug=?",
                    rows_to_update,
                )
            conn.commit()
            bar.update(len(results))
            time.sleep(0.05)  # be gentle — ~20 req/s max

    print(f"\nSync done: {inserted:,} new  |  {updated:,} updated  |  {skipped:,} already current")
    conn.close()


def _fetch_and_store(slug: str, conn: sqlite3.Connection, lock: threading.Lock) -> str:
    """Fetch content for one slug and write to DB. Returns 'ok', 'missing', or 'error'."""
    try:
        data = content_for_slug(slug)
        if data is None:
            return "missing"

        details = data.get("details", {})
        meta = details.get("metadata", {})
        attachments = details.get("attachments", [])
        pdf_url = next(
            (a["url"] for a in attachments if a.get("content_type") == "application/pdf"),
            None,
        )

        row = (
            datetime.now(timezone.utc).isoformat(),
            meta.get("tribunal_decision_decision_date"),
            meta.get("tribunal_decision_country"),
            json.dumps(meta.get("tribunal_decision_categories", [])),
            meta.get("hidden_indexable_content"),
            pdf_url,
            data.get("content_id"),
            slug,
        )

        with lock:
            conn.execute(
                """UPDATE decisions
                   SET fetched_at=?, decision_date=?, country=?, categories=?,
                       full_text=?, pdf_url=?, content_id=?
                   WHERE slug=?""",
                row,
            )
            conn.commit()
        return "ok"
    except Exception:
        return "error"


def cmd_fetch_all(args):
    """Fetch full content for every decision not yet fetched (or re-fetch all with --refetch)."""
    conn = open_db()
    lock = threading.Lock()

    query = "SELECT slug FROM decisions WHERE fetched_at IS NULL ORDER BY published_at DESC"
    if args.refetch:
        query = "SELECT slug FROM decisions ORDER BY published_at DESC"

    slugs = [r["slug"] for r in conn.execute(query).fetchall()]
    if not slugs:
        print("Nothing to fetch — all decisions already have full content.")
        conn.close()
        return

    workers = args.workers
    print(f"Fetching full content for {len(slugs):,} decisions ({workers} parallel workers)…")
    print("This will take a while for the full dataset. Press Ctrl+C to stop; progress is saved.\n")

    ok = missing = errors = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_and_store, slug, conn, lock): slug for slug in slugs}
        with tqdm(total=len(slugs), desc="Fetching content", unit="decisions") as bar:
            for future in as_completed(futures):
                result = future.result()
                if result == "ok":
                    ok += 1
                elif result == "missing":
                    missing += 1
                else:
                    errors += 1
                bar.update(1)
                bar.set_postfix(ok=ok, missing=missing, err=errors)

    print(f"\nDone: {ok:,} fetched  |  {missing:,} gone (404)  |  {errors:,} errors")
    conn.close()


def cmd_fetch(args):
    """Fetch full content for a single decision by slug."""
    conn = open_db()
    lock = threading.Lock()
    slug = args.slug
    if not slug.startswith("/"):
        slug = f"/employment-tribunal-decisions/{slug}"

    # Ensure it's in the index
    existing = conn.execute("SELECT slug FROM decisions WHERE slug=?", (slug,)).fetchone()
    if existing is None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO decisions (slug, indexed_at) VALUES (?,?)", (slug, now)
        )
        conn.commit()

    print(f"Fetching: {slug}")
    result = _fetch_and_store(slug, conn, lock)

    if result == "ok":
        row = conn.execute("SELECT * FROM decisions WHERE slug=?", (slug,)).fetchone()
        print(f"\nTitle:         {row['title']}")
        print(f"Decision date: {row['decision_date']}")
        print(f"Country:       {row['country']}")
        print(f"Categories:    {row['categories']}")
        print(f"PDF URL:       {row['pdf_url']}")
        text_len = len(row["full_text"] or "")
        print(f"Full text:     {text_len:,} characters")
    elif result == "missing":
        print("Decision not found (404).")
    else:
        print("Error fetching decision.", file=sys.stderr)
        sys.exit(1)

    conn.close()


def cmd_stats(args):
    conn = open_db()
    total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    fetched = conn.execute("SELECT COUNT(*) FROM decisions WHERE fetched_at IS NOT NULL").fetchone()[0]
    has_text = conn.execute("SELECT COUNT(*) FROM decisions WHERE full_text IS NOT NULL").fetchone()[0]

    print(f"Decisions in index:         {total:>10,}")
    print(f"With full content fetched:  {fetched:>10,}")
    print(f"With full text:             {has_text:>10,}")
    print(f"Awaiting fetch:             {total - fetched:>10,}")

    if fetched > 0:
        print()
        print("--- By country ---")
        for row in conn.execute(
            "SELECT country, COUNT(*) n FROM decisions WHERE fetched_at IS NOT NULL GROUP BY country ORDER BY n DESC"
        ):
            print(f"  {(row['country'] or 'unknown'):<20} {row['n']:>8,}")

        print()
        print("--- Decisions per year (decision date) ---")
        for row in conn.execute(
            """SELECT substr(decision_date,1,4) yr, COUNT(*) n
               FROM decisions WHERE decision_date IS NOT NULL
               GROUP BY yr ORDER BY yr DESC LIMIT 15"""
        ):
            print(f"  {row['yr']}  {row['n']:>8,}")

    conn.close()


def cmd_export(args):
    conn = open_db()
    out_path = Path(args.output)
    query = "SELECT * FROM decisions"
    if args.fetched_only:
        query += " WHERE fetched_at IS NOT NULL"
    query += " ORDER BY published_at DESC"

    rows = conn.execute(query).fetchall()
    print(f"Exporting {len(rows):,} decisions to {out_path}…")
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            record = dict(row)
            if record.get("categories"):
                record["categories"] = json.loads(record["categories"])
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Done — {out_path} ({out_path.stat().st_size / 1_048_576:.1f} MB)")
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Employment Tribunal Decisions scraper (GOV.UK)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Sync the decision index from GOV.UK")

    p_fa = sub.add_parser("fetch-all", help="Fetch full content for all indexed decisions")
    p_fa.add_argument("--workers", type=int, default=5, help="Parallel HTTP workers (default 5)")
    p_fa.add_argument("--refetch", action="store_true", help="Re-fetch even already-fetched decisions")

    p_f = sub.add_parser("fetch", help="Fetch full content for one decision")
    p_f.add_argument("slug", help="Decision slug or full path, e.g. mr-smith-v-acme-1234/2024")

    sub.add_parser("stats", help="Show DB statistics")

    p_ex = sub.add_parser("export", help="Export to newline-delimited JSON")
    p_ex.add_argument("--output", default="decisions.jsonl", help="Output file (default: decisions.jsonl)")
    p_ex.add_argument("--fetched-only", action="store_true", help="Only export decisions with full content")

    args = parser.parse_args()
    {
        "sync": cmd_sync,
        "fetch-all": cmd_fetch_all,
        "fetch": cmd_fetch,
        "stats": cmd_stats,
        "export": cmd_export,
    }[args.command](args)


if __name__ == "__main__":
    main()
