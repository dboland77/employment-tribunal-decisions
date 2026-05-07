#!/usr/bin/env python3
"""
Employment Tribunal / Employment Appeal Tribunal Decisions Scraper

Commands:
  sync          Fetch all decision slugs from GOV.UK into the local DB index
  fetch-all     Fetch full content (text, metadata) for every indexed decision
  fetch <slug>  Fetch full content for a single decision
  tag-outcomes  Analyse full text and set claimant/respondent won/appealed flags
  stats         Show database statistics
  export        Export DB to newline-delimited JSON (decisions.jsonl)
"""
import argparse
import json
import re
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
DB_PATH = Path("/mnt/data/decisions.db")

# Supported tribunal types
TRIBUNALS = {
    "et": {
        "document_type": "employment_tribunal_decision",
        "slug_prefix": "/employment-tribunal-decisions",
        "label": "Employment Tribunal",
    },
    "eat": {
        "document_type": "employment_appeal_tribunal_decision",
        "slug_prefix": "/employment-appeal-tribunal-decisions",
        "label": "Employment Appeal Tribunal",
    },
}

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
            tribunal_type   TEXT,               -- 'et' or 'eat'
            decision_date   TEXT,
            country         TEXT,
            categories      TEXT,   -- JSON array e.g. ["unfair-dismissal"]
            full_text       TEXT,
            pdf_url         TEXT,
            content_id      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_published     ON decisions(published_at);
        CREATE INDEX IF NOT EXISTS idx_decision_date ON decisions(decision_date);
        CREATE INDEX IF NOT EXISTS idx_country       ON decisions(country);
    """)
    # Migrate existing DBs that predate the tribunal_type column
    try:
        conn.execute("ALTER TABLE decisions ADD COLUMN tribunal_type TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.executescript("CREATE INDEX IF NOT EXISTS idx_tribunal_type ON decisions(tribunal_type);")
    # Backfill tribunal_type for rows synced before this column existed
    conn.execute("UPDATE decisions SET tribunal_type='et' WHERE tribunal_type IS NULL AND slug LIKE '/employment-tribunal-decisions/%'")
    conn.execute("UPDATE decisions SET tribunal_type='eat' WHERE tribunal_type IS NULL AND slug LIKE '/employment-appeal-tribunal-decisions/%'")
    # Migrate: add outcome/appeal flag columns
    for col in ("claimant_appealed", "respondent_appealed", "claimant_won", "respondent_won"):
        try:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass
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


def search_page(start: int, document_type: str) -> dict:
    return _get(SEARCH_API, {
        "filter_content_store_document_type": document_type,
        "count": PAGE_SIZE,
        "start": start,
        "fields": "title,link,public_timestamp",
        "order": "-public_timestamp",
    })


def content_for_slug(slug: str, slug_prefix: str) -> dict | None:
    """Fetch full content from the GOV.UK Content API. Returns None if 404."""
    path = slug if slug.startswith("/") else f"{slug_prefix}/{slug}"
    return _get(f"{CONTENT_API}{path}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _resolve_tribunals(tribunal_arg: str) -> list[str]:
    """Return list of tribunal keys from --tribunal argument."""
    if tribunal_arg == "all":
        return list(TRIBUNALS.keys())
    if tribunal_arg not in TRIBUNALS:
        sys.exit(f"Unknown tribunal type: {tribunal_arg!r}. Choose from: et, eat, all")
    return [tribunal_arg]


def cmd_sync(args):
    """Walk every search page and upsert slugs into the local index."""
    conn = open_db()
    now = datetime.now(timezone.utc).isoformat()
    keys = _resolve_tribunals(args.tribunal)

    for key in keys:
        t = TRIBUNALS[key]
        print(f"\n=== Syncing {t['label']} ({key}) ===")

        first = search_page(0, t["document_type"])
        total = first["total"]
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print(f"Remote total: {total:,} decisions across {pages} pages")

        inserted = updated = skipped = 0

        with tqdm(total=total, desc="Syncing index", unit="decisions") as bar:
            for page_num in range(pages):
                data = search_page(page_num * PAGE_SIZE, t["document_type"])
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
                        rows_to_insert.append((slug, title, published_at, now, key))
                        inserted += 1
                    elif existing["published_at"] != published_at:
                        rows_to_update.append((title, published_at, slug))
                        updated += 1
                    else:
                        skipped += 1

                if rows_to_insert:
                    conn.executemany(
                        "INSERT OR IGNORE INTO decisions (slug, title, published_at, indexed_at, tribunal_type) VALUES (?,?,?,?,?)",
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

        print(f"Sync done: {inserted:,} new  |  {updated:,} updated  |  {skipped:,} already current")

    conn.close()


def _fetch_and_store(slug: str, slug_prefix: str, tribunal_type: str,
                     conn: sqlite3.Connection, lock: threading.Lock) -> str:
    """Fetch content for one slug and write to DB. Returns 'ok', 'missing', or 'error'."""
    try:
        data = content_for_slug(slug, slug_prefix)
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
            tribunal_type,
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
                   SET fetched_at=?, tribunal_type=?, decision_date=?, country=?, categories=?,
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
    keys = _resolve_tribunals(args.tribunal)

    # Build slug-prefix lookup per slug using tribunal_type stored in DB
    type_filter = f"AND tribunal_type IN ({','.join('?'*len(keys))})"
    base_query = f"SELECT slug, tribunal_type FROM decisions WHERE fetched_at IS NULL {type_filter} ORDER BY published_at DESC"
    if args.refetch:
        base_query = f"SELECT slug, tribunal_type FROM decisions WHERE 1 {type_filter} ORDER BY published_at DESC"

    rows = conn.execute(base_query, keys).fetchall()
    if not rows:
        print("Nothing to fetch — all decisions already have full content.")
        conn.close()
        return

    workers = args.workers
    print(f"Fetching full content for {len(rows):,} decisions ({workers} parallel workers)…")
    print("This will take a while for the full dataset. Press Ctrl+C to stop; progress is saved.\n")

    ok = missing = errors = 0

    def _task(row):
        key = row["tribunal_type"] or "et"  # default for legacy rows without tribunal_type
        prefix = TRIBUNALS.get(key, TRIBUNALS["et"])["slug_prefix"]
        return _fetch_and_store(row["slug"], prefix, key, conn, lock)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_task, row): row["slug"] for row in rows}
        with tqdm(total=len(rows), desc="Fetching content", unit="decisions") as bar:
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

    key = args.tribunal
    t = TRIBUNALS[key]
    slug = args.slug
    if not slug.startswith("/"):
        slug = f"{t['slug_prefix']}/{slug}"

    # Ensure it's in the index
    existing = conn.execute("SELECT slug FROM decisions WHERE slug=?", (slug,)).fetchone()
    if existing is None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO decisions (slug, indexed_at, tribunal_type) VALUES (?,?,?)",
            (slug, now, key),
        )
        conn.commit()

    print(f"Fetching: {slug}")
    result = _fetch_and_store(slug, t["slug_prefix"], key, conn, lock)

    if result == "ok":
        row = conn.execute("SELECT * FROM decisions WHERE slug=?", (slug,)).fetchone()
        print(f"\nTribunal:      {t['label']}")
        print(f"Title:         {row['title']}")
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


# ---------------------------------------------------------------------------
# Outcome flag detection
# ---------------------------------------------------------------------------

# ET: claimant won at least one claim
_ET_CLAIMANT_WIN = re.compile(
    r'(?i)(?:'
    r'(?:is|are)\s+well[- ]?founded'
    r'|ordered\s+to\s+pay'
    r'|(?:claim|complaint)s?\s+(?:(?:in|of)\s+\w+(?:\s+\w+){0,4}\s+)?succeed'
    r'|finds?\s+(?:in\s+)?favour\s+of\s+(?:the\s+)?claimant'
    r'|claimant\s+(?:is\s+)?entitled\s+to\s+(?:compensation|damages|remedy|award)'
    r')'
)

# ET: respondent won at least one claim (or struck out / no jurisdiction)
_ET_RESPONDENT_WIN = re.compile(
    r'(?i)(?:'
    r'not\s+well[- ]?founded'
    r'|(?:claim|complaint|action)s?\s+(?:is|are)\s+(?:therefore\s+|accordingly\s+|hereby\s+)?dismissed'
    r'|judgment\s+(?:is\s+)?(?:to\s+)?dismiss(?:ed)?\s+(?:the\s+)?claim'
    r'|struck\s+out'
    r'|does\s+not\s+succeed\s+and\s+(?:is\s+)?dismissed'
    r')'
)

# Withdrawal — exclude from outcome detection
_WITHDRAWAL = re.compile(r'(?i)dismissed\s+(?:following|on|after|upon)\s+(?:a\s+)?withdrawal')

# EAT: who brought the appeal
_EAT_CLAIMANT_APPEALED = re.compile(
    r"(?i)(?:"
    r"claimant['’]?s?\s+appeal"
    r"|claimant\s+(?:has\s+)?appeal"
    r"|appeal\s+(?:by|of|from)\s+(?:the\s+)?claimant"
    r"|employee['’]?s?\s+appeal"
    r"|claimant[,\s]\s*appellant"
    r"|appellant[,\s]\s*claimant"
    r")"
)

_EAT_RESPONDENT_APPEALED = re.compile(
    r"(?i)(?:"
    r"respondent['’]?s?\s+appeal"
    r"|respondent\s+(?:has\s+)?appeal"
    r"|appeal\s+(?:by|of|from)\s+(?:the\s+)?respondent"
    r"|employer['’]?s?\s+appeal"
    r"|respondent[,\s]\s*appellant"
    r"|appellant[,\s]\s*respondent"
    r")"
)

# EAT: outcome of the appeal
_EAT_ALLOWED = re.compile(
    r'(?i)(?:'
    r'\bheld\s*[:\-]\s*allowing\b'
    r'|\ballow(?:ing|ed)\s+the\s+appeal\b'
    r'|\bappeal\s+(?:is\s+|was\s+)?allow(?:ed|s)\b'
    r')'
)

_EAT_DISMISSED = re.compile(
    r'(?i)(?:'
    r'\bheld\s*[:\-]\s*dismissing\b'
    r'|\bdismiss(?:ing|ed)\s+the\s+appeal\b'
    r'|\bappeal\s+(?:is\s+|was\s+)?dismiss(?:ed|es)\b'
    r'|\bappeal\s+(?:is\s+)?rejected\b'
    r'|\beat\s+(?:rejected|dismissed)\s+(?:the\s+)?appeal\b'
    r')'
)


def _detect_outcome_flags(tribunal_type: str, full_text: str) -> dict:
    """Return dict of claimant_appealed, respondent_appealed, claimant_won, respondent_won (each 0/1/None)."""
    flags = dict(claimant_appealed=None, respondent_appealed=None,
                 claimant_won=None, respondent_won=None)
    if not full_text:
        return flags

    head = full_text[:8000]

    if tribunal_type == 'eat':
        ca = bool(_EAT_CLAIMANT_APPEALED.search(head))
        ra = bool(_EAT_RESPONDENT_APPEALED.search(head))
        flags['claimant_appealed'] = int(ca)
        flags['respondent_appealed'] = int(ra)

        allowed   = bool(_EAT_ALLOWED.search(head))
        dismissed = bool(_EAT_DISMISSED.search(head))

        # Derive won/lost only for unambiguous single-side appeals
        if allowed and not dismissed:
            if ca and not ra:
                flags['claimant_won'] = 1; flags['respondent_won'] = 0
            elif ra and not ca:
                flags['respondent_won'] = 1; flags['claimant_won'] = 0
        elif dismissed and not allowed:
            if ca and not ra:
                flags['claimant_won'] = 0; flags['respondent_won'] = 1
            elif ra and not ca:
                flags['respondent_won'] = 0; flags['claimant_won'] = 1

    else:  # ET
        flags['claimant_appealed'] = None
        flags['respondent_appealed'] = None

        # Strip withdrawal sentences so they don't trigger outcome flags
        txt = _WITHDRAWAL.sub('', head)

        flags['claimant_won']   = int(bool(_ET_CLAIMANT_WIN.search(txt)))
        flags['respondent_won'] = int(bool(_ET_RESPONDENT_WIN.search(txt)))

    return flags


def cmd_tag_outcomes(args):
    """Analyse full_text for every fetched decision and write outcome/appeal flags."""
    conn = open_db()

    rows = conn.execute(
        "SELECT rowid, tribunal_type, full_text FROM decisions WHERE full_text IS NOT NULL"
    ).fetchall()

    if not rows:
        print("No decisions with full text found.")
        conn.close()
        return

    print(f"Tagging outcomes for {len(rows):,} decisions…")

    updated = 0
    for row in tqdm(rows, unit="decisions"):
        flags = _detect_outcome_flags(row['tribunal_type'], row['full_text'])
        conn.execute(
            """UPDATE decisions
               SET claimant_appealed=?, respondent_appealed=?,
                   claimant_won=?, respondent_won=?
               WHERE rowid=?""",
            (flags['claimant_appealed'], flags['respondent_appealed'],
             flags['claimant_won'], flags['respondent_won'],
             row['rowid']),
        )
        updated += 1
        if updated % 5000 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    # Print summary stats
    conn2 = open_db()
    for col in ('claimant_won', 'respondent_won', 'claimant_appealed', 'respondent_appealed'):
        n = conn2.execute(f"SELECT COUNT(*) FROM decisions WHERE {col}=1").fetchone()[0]
        print(f"  {col}=1 : {n:,}")
    conn2.close()
    print("Done.")


def cmd_stats(args):
    conn = open_db()
    keys = _resolve_tribunals(args.tribunal)
    type_filter = f"AND tribunal_type IN ({','.join('?'*len(keys))})"

    total = conn.execute(
        f"SELECT COUNT(*) FROM decisions WHERE 1 {type_filter}", keys
    ).fetchone()[0]
    fetched = conn.execute(
        f"SELECT COUNT(*) FROM decisions WHERE fetched_at IS NOT NULL {type_filter}", keys
    ).fetchone()[0]
    has_text = conn.execute(
        f"SELECT COUNT(*) FROM decisions WHERE full_text IS NOT NULL {type_filter}", keys
    ).fetchone()[0]

    print(f"Decisions in index:         {total:>10,}")
    print(f"With full content fetched:  {fetched:>10,}")
    print(f"With full text:             {has_text:>10,}")
    print(f"Awaiting fetch:             {total - fetched:>10,}")

    if fetched > 0:
        print()
        print("--- By tribunal type ---")
        for row in conn.execute(
            f"SELECT tribunal_type, COUNT(*) n FROM decisions WHERE fetched_at IS NOT NULL {type_filter} GROUP BY tribunal_type ORDER BY n DESC",
            keys,
        ):
            label = TRIBUNALS.get(row["tribunal_type"] or "", {}).get("label", "unknown")
            print(f"  {label:<30} {row['n']:>8,}")

        print()
        print("--- By country ---")
        for row in conn.execute(
            f"SELECT country, COUNT(*) n FROM decisions WHERE fetched_at IS NOT NULL {type_filter} GROUP BY country ORDER BY n DESC",
            keys,
        ):
            print(f"  {(row['country'] or 'unknown'):<30} {row['n']:>8,}")

        print()
        print("--- Decisions per year (decision date) ---")
        for row in conn.execute(
            f"""SELECT substr(decision_date,1,4) yr, COUNT(*) n
               FROM decisions WHERE decision_date IS NOT NULL {type_filter}
               GROUP BY yr ORDER BY yr DESC LIMIT 15""",
            keys,
        ):
            print(f"  {row['yr']}  {row['n']:>8,}")

    conn.close()


def cmd_export(args):
    conn = open_db()
    out_path = Path(args.output)
    keys = _resolve_tribunals(args.tribunal)
    type_filter = f"AND tribunal_type IN ({','.join('?'*len(keys))})"

    query = f"SELECT * FROM decisions WHERE 1 {type_filter}"
    if args.fetched_only:
        query += " AND fetched_at IS NOT NULL"
    query += " ORDER BY published_at DESC"

    rows = conn.execute(query, keys).fetchall()
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
        description="Employment Tribunal / Appeal Tribunal Decisions scraper (GOV.UK)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Sync the decision index from GOV.UK")
    p_sync.add_argument(
        "--tribunal", choices=["et", "eat", "all"], default="all",
        help="Tribunal type to sync (default: all)",
    )

    p_fa = sub.add_parser("fetch-all", help="Fetch full content for all indexed decisions")
    p_fa.add_argument("--workers", type=int, default=5, help="Parallel HTTP workers (default 5)")
    p_fa.add_argument("--refetch", action="store_true", help="Re-fetch even already-fetched decisions")
    p_fa.add_argument(
        "--tribunal", choices=["et", "eat", "all"], default="all",
        help="Tribunal type to fetch (default: all)",
    )

    p_f = sub.add_parser("fetch", help="Fetch full content for one decision")
    p_f.add_argument("slug", help="Decision slug or full path")
    p_f.add_argument(
        "--tribunal", choices=["et", "eat"], default="et",
        help="Tribunal type for bare slugs (default: et)",
    )

    p_stats = sub.add_parser("stats", help="Show DB statistics")
    p_stats.add_argument(
        "--tribunal", choices=["et", "eat", "all"], default="all",
        help="Tribunal type to show stats for (default: all)",
    )

    sub.add_parser("tag-outcomes", help="Detect and tag claimant/respondent won/appealed flags")

    p_ex = sub.add_parser("export", help="Export to newline-delimited JSON")
    p_ex.add_argument("--output", default="decisions.jsonl", help="Output file (default: decisions.jsonl)")
    p_ex.add_argument("--fetched-only", action="store_true", help="Only export decisions with full content")
    p_ex.add_argument(
        "--tribunal", choices=["et", "eat", "all"], default="all",
        help="Tribunal type to export (default: all)",
    )

    args = parser.parse_args()
    {
        "sync": cmd_sync,
        "fetch-all": cmd_fetch_all,
        "fetch": cmd_fetch,
        "tag-outcomes": cmd_tag_outcomes,
        "stats": cmd_stats,
        "export": cmd_export,
    }[args.command](args)


if __name__ == "__main__":
    main()
