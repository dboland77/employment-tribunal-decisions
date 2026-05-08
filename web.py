#!/usr/bin/env python3
"""Searchable web interface for the Employment Tribunal decisions database."""

import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, abort, render_template_string, request

DB_PATH = Path("/mnt/data/decisions.db")
PER_PAGE = 25
PORT = 8080

app = Flask(__name__)
_fts_ready = threading.Event()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def open_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def build_fts_index():
    """Create FTS5 virtual table and populate it if empty. Runs once at startup."""
    conn = open_db()
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
            title,
            full_text,
            content='decisions',
            content_rowid='rowid',
            tokenize='porter ascii'
        )
    """)
    conn.commit()
    # decisions_fts_docsize is only non-empty when the FTS term index has
    # actually been built. SELECT COUNT(*) FROM decisions_fts reads from the
    # content table (decisions) and returns a misleadingly large number even
    # when the index is completely empty.
    n = conn.execute("SELECT COUNT(*) FROM decisions_fts_docsize").fetchone()[0]
    if n == 0:
        print("Building full-text search index — this may take a few minutes…", flush=True)
        conn.execute("INSERT INTO decisions_fts(decisions_fts) VALUES('rebuild')")
        conn.commit()
        print("Search index ready.", flush=True)
    conn.close()
    _fts_ready.set()


threading.Thread(target=build_fts_index, daemon=True).start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_fts_query(q: str) -> str:
    """Convert plain text to a safe FTS5 query (implicit AND on all terms)."""
    words = q.strip().split()
    return " ".join(f'"{w}"' for w in words if w)


def page_url(**overrides):
    """Build a search URL preserving current args, with overrides applied."""
    args = {k: v for k, v in request.args.items()}
    args.update({k: v for k, v in overrides.items() if v is not None})
    args = {k: v for k, v in args.items() if v}
    return "/?" + urlencode(args)


def build_filters(tribunal, country, category, year, outcome, prefix=""):
    """Return (sql_fragments, params) for the given filter values."""
    parts, params = [], []
    if tribunal:
        parts.append(f"{prefix}tribunal_type = ?")
        params.append(tribunal)
    if country and tribunal not in ("eat", "ca"):
        parts.append(f"{prefix}country = ?")
        params.append(country)
    if category:
        parts.append(f"EXISTS (SELECT 1 FROM json_each({prefix}categories) WHERE value = ?)")
        params.append(category)
    if year:
        parts.append(f"substr({prefix}decision_date, 1, 4) = ?")
        params.append(year)
    if outcome:
        parts.append(f"{prefix}{outcome} = 1")
    return parts, params


def fmt_category(slug: str) -> str:
    return slug.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: #f4f5f7; color: #222; min-height: 100vh; }
a { color: #1a4a8a; }
header { background: #1a3a5c; color: white; padding: 1rem 2rem; }
header h1 { font-size: 1.4rem; font-weight: 600; }
header p { font-size: 0.85rem; opacity: 0.75; margin-top: 0.2rem; }
header nav { margin-top: 0.5rem; display: flex; gap: 1.2rem; }
header nav a { color: rgba(255,255,255,0.7); font-size: 0.82rem; text-decoration: none; }
header nav a:hover { color: white; }
header nav a.active { color: white; font-weight: 600; border-bottom: 2px solid rgba(255,255,255,0.5); padding-bottom: 1px; }
.container { max-width: 1100px; margin: 0 auto; padding: 1.5rem 2rem; }
.search-box { background: white; border-radius: 8px; padding: 1.2rem 1.4rem;
              box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 1.4rem; }
.search-row { display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center; }
.search-row input[type=text] { flex: 1; min-width: 220px; padding: 0.55rem 0.9rem;
  border: 1px solid #ccc; border-radius: 5px; font-size: 1rem; }
.search-row input[type=text]:focus { outline: none; border-color: #1a4a8a; box-shadow: 0 0 0 2px #d0e4ff; }
.search-row select { padding: 0.55rem 0.8rem; border: 1px solid #ccc; border-radius: 5px;
  font-size: 0.9rem; background: white; }
.search-row button { padding: 0.55rem 1.4rem; background: #1a3a5c; color: white;
  border: none; border-radius: 5px; font-size: 1rem; cursor: pointer; white-space: nowrap; }
.search-row button:hover { background: #2a5a8c; }
.notice { background: #fff8e1; border-left: 4px solid #ffc107; border-radius: 4px;
          padding: 0.6rem 1rem; margin-bottom: 1rem; font-size: 0.88rem; color: #5a4000; }
.results-header { font-size: 0.88rem; color: #666; margin-bottom: 0.8rem; }
.result { background: white; border-radius: 7px; padding: 1rem 1.3rem; margin-bottom: 0.75rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.07); }
.result h2 { font-size: 1rem; font-weight: 600; margin-bottom: 0.3rem; line-height: 1.4;
             display: flex; align-items: flex-start; gap: 0.3rem; }
.result h2 a { color: #1a3a5c; text-decoration: none; flex: 1; }
.result h2 a:hover { text-decoration: underline; }
.meta { font-size: 0.8rem; color: #666; display: flex; gap: 0.8rem; flex-wrap: wrap;
        align-items: center; margin-bottom: 0.4rem; }
.tag { display: inline-block; border-radius: 3px; padding: 1px 7px; font-size: 0.75rem; font-weight: 600; }
.tag.et  { background: #e3f2fd; color: #0d47a1; }
.tag.eat { background: #fce4ec; color: #880e4f; }
.tag.cat { background: #f3e5f5; color: #6a1b9a; }
.tag.ca  { background: #e8f5e9; color: #1b5e20; }
.snippet { font-size: 0.85rem; color: #444; line-height: 1.55; }
.snippet mark { background: #fff176; font-style: normal; border-radius: 2px; padding: 0 1px; }
.empty { padding: 2rem 0; color: #888; font-size: 0.95rem; }
.pagination { display: flex; gap: 0.35rem; justify-content: center; margin-top: 1.5rem; flex-wrap: wrap; }
.pagination a, .pagination span { display: inline-block; padding: 0.4rem 0.75rem; border-radius: 5px;
  border: 1px solid #ccc; font-size: 0.9rem; text-decoration: none; color: #333; background: white; }
.pagination .cur { background: #1a3a5c; color: white; border-color: #1a3a5c; font-weight: 600; }
.pagination .gap { border-color: transparent; background: none; }
.pagination a:hover { background: #eef3ff; border-color: #1a4a8a; }
/* detail page */
.back { display: inline-block; margin-bottom: 1.2rem; font-size: 0.9rem; text-decoration: none; }
.back:hover { text-decoration: underline; }
.decision-meta { background: white; border-radius: 8px; padding: 1.2rem 1.5rem;
                 box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 1.5rem; }
.decision-meta h2 { font-size: 1.15rem; margin-bottom: 0.5rem; color: #1a3a5c; line-height: 1.4; }
.decision-meta table { border-collapse: collapse; width: 100%; }
.decision-meta td { padding: 0.35rem 0.5rem; font-size: 0.9rem; vertical-align: top; border-bottom: 1px solid #f0f0f0; }
.decision-meta td:first-child { font-weight: 600; width: 150px; color: #555; white-space: nowrap; }
.full-text { background: white; border-radius: 8px; padding: 1.5rem 2rem;
             box-shadow: 0 1px 4px rgba(0,0,0,.1); white-space: pre-wrap;
             font-family: Georgia, 'Times New Roman', serif; font-size: 0.92rem; line-height: 1.75;
             word-wrap: break-word; }
/* pin / save */
.pin-btn { background: none; border: none; cursor: pointer; font-size: 1.15rem;
           color: #ccc; padding: 0 0.1rem; line-height: 1; flex-shrink: 0;
           transition: color 0.1s; }
.pin-btn:hover { color: #e6a817; }
.pin-btn.pinned { color: #e6a817; }
.pin-detail { display: inline-block; margin-bottom: 0.8rem; font-size: 0.95rem; }
.pin-detail .pin-btn { font-size: 1.2rem; vertical-align: middle; margin-right: 0.3rem; }
"""

# Shared localStorage pin logic, injected into every page via {{ shared_js|safe }}.
# afterPinToggle is an optional page-specific hook called after each toggle.
SHARED_JS = """<script>
(function () {
  var KEY = 'et_pins';
  function getPins() { return JSON.parse(localStorage.getItem(KEY) || '[]'); }
  function savePins(p) { localStorage.setItem(KEY, JSON.stringify(p)); }

  function renderBtn(btn, pinned) {
    btn.textContent = pinned ? '★' : '☆';
    btn.title = pinned ? 'Remove from saved' : 'Save for later';
    pinned ? btn.classList.add('pinned') : btn.classList.remove('pinned');
  }

  function syncCount() {
    var n = getPins().length;
    var el = document.getElementById('saved-nav-link');
    if (el) el.textContent = n > 0 ? 'Saved (' + n + ')' : 'Saved';
  }

  window.togglePin = function (btn, event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    var slug = btn.dataset.slug;
    var pins = getPins();
    var idx = pins.indexOf(slug);
    var nowPinned = idx === -1;
    if (nowPinned) pins.push(slug); else pins.splice(idx, 1);
    savePins(pins);
    renderBtn(btn, nowPinned);
    syncCount();
    if (typeof window.afterPinToggle === 'function') window.afterPinToggle(slug, nowPinned, btn);
  };

  document.addEventListener('DOMContentLoaded', function () {
    var pins = getPins();
    document.querySelectorAll('.pin-btn').forEach(function (b) {
      renderBtn(b, pins.indexOf(b.dataset.slug) !== -1);
    });
    syncCount();
  });
})();
</script>"""

SEARCH_TMPL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Employment Tribunal Decisions</title>
  <style>{{ style }}</style>
</head>
<body>
<header>
  <h1>Employment Tribunal Decisions</h1>
  <p>{{ '{:,}'.format(db_total) }} decisions indexed from GOV.UK</p>
  <nav>
    <a href="/" class="active">Search</a>
    <a href="/pinned" id="saved-nav-link">Saved</a>
  </nav>
</header>
<div class="container">
  <div class="search-box">
    <form method="get" action="/">
      <div class="search-row">
        <input type="text" name="q" value="{{ q|e }}" placeholder="Search decisions…" autofocus>
        <select name="tribunal">
          <option value="">All tribunals</option>
          <option value="et"  {% if tribunal=='et'  %}selected{% endif %}>Employment Tribunal</option>
          <option value="eat" {% if tribunal=='eat' %}selected{% endif %}>Appeal Tribunal</option>
          <option value="ca"  {% if tribunal=='ca'  %}selected{% endif %}>Court of Appeal</option>
        </select>
        <select name="country">
          <option value="">All countries</option>
          <option value="england-and-wales" {% if country=='england-and-wales' %}selected{% endif %}>England &amp; Wales</option>
          <option value="scotland"          {% if country=='scotland'          %}selected{% endif %}>Scotland</option>
        </select>
        <select name="category">
          <option value="">All claim types</option>
          {% for cat in all_categories %}
          <option value="{{ cat }}" {% if category==cat %}selected{% endif %}>{{ fmt_category(cat) }}</option>
          {% endfor %}
        </select>
        <select name="year">
          <option value="">All years</option>
          {% for yr in all_years %}
          <option value="{{ yr }}" {% if year==yr %}selected{% endif %}>{{ yr }}</option>
          {% endfor %}
        </select>
        <select name="outcome">
          <option value="">All outcomes</option>
          <option value="claimant_won"        {% if outcome=='claimant_won'        %}selected{% endif %}>Claimant won</option>
          <option value="respondent_won"      {% if outcome=='respondent_won'      %}selected{% endif %}>Respondent won</option>
          <option value="claimant_appealed"   {% if outcome=='claimant_appealed'   %}selected{% endif %}>Claimant appealed</option>
          <option value="respondent_appealed" {% if outcome=='respondent_appealed' %}selected{% endif %}>Respondent appealed</option>
        </select>
        <button type="submit">Search</button>
      </div>
    </form>
  </div>
  <script>
    (function () {
      var tribunal = document.querySelector('select[name="tribunal"]');
      var country  = document.querySelector('select[name="country"]');
      function sync() {
        var noCountry = tribunal.value === 'eat' || tribunal.value === 'ca';
        country.disabled = noCountry;
        if (noCountry) country.value = '';
      }
      tribunal.addEventListener('change', sync);
      sync();
    })();
  </script>

  {% if not fts_ready %}
  <div class="notice">
    Search index is still building — full-text search will be available shortly.
    Browsing all decisions by date in the meantime.
  </div>
  {% endif %}

  <div class="results-header">
    {% if q %}
      <strong>{{ '{:,}'.format(total) }}</strong> result{{ 's' if total != 1 }} for
      &ldquo;<strong>{{ q|e }}</strong>&rdquo;
    {% else %}
      <strong>{{ '{:,}'.format(total) }}</strong> decision{{ 's' if total != 1 }}
    {% endif %}
    {% if pages > 1 %}&nbsp;&mdash;&nbsp;page {{ page }} of {{ pages }}{% endif %}
  </div>

  {% if results %}
    {% for r in results %}
    <div class="result">
      <h2><a href="/decision{{ r['slug'] }}">{{ r['title'] or r['slug'] }}</a>
          <button class="pin-btn" data-slug="{{ r['slug'] }}"
                  onclick="togglePin(this,event)" title="Save for later">&#9734;</button></h2>
      <div class="meta">
        {% if r['decision_date'] %}<span>{{ r['decision_date'] }}</span>{% endif %}
        {% if r['tribunal_type'] %}
          <span class="tag {{ r['tribunal_type'] }}">
            {{ 'Employment Tribunal' if r['tribunal_type']=='et' else ('Appeal Tribunal' if r['tribunal_type']=='eat' else 'Court of Appeal') }}
          </span>
        {% endif %}
        {% if r['country'] %}<span>{{ r['country'].replace('-',' ').title() }}</span>{% endif %}
        {% if r['neutral_citation'] %}<span style="font-style:italic">{{ r['neutral_citation'] }}</span>{% endif %}
        {% if r['categories'] %}
          {% for cat in json.loads(r['categories']) %}
            <span class="tag cat">{{ fmt_category(cat) }}</span>
          {% endfor %}
        {% endif %}
      </div>
      {% if r['snippet'] %}
      <div class="snippet">…{{ r['snippet']|safe }}…</div>
      {% endif %}
    </div>
    {% endfor %}
  {% else %}
    <p class="empty">No decisions found.</p>
  {% endif %}

  {% if pages > 1 %}
  <div class="pagination">
    {% for p in range(1, pages + 1) %}
      {% if p == page %}
        <span class="cur">{{ p }}</span>
      {% elif p == 1 or p == pages or (page - 2 <= p <= page + 2) %}
        <a href="{{ page_url(page=p) }}">{{ p }}</a>
      {% elif p == page - 3 or p == page + 3 %}
        <span class="gap">…</span>
      {% endif %}
    {% endfor %}
  </div>
  {% endif %}
</div>
{{ shared_js|safe }}
</body>
</html>
"""

DETAIL_TMPL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ d['title'] or 'Decision' }} — ET Decisions</title>
  <style>{{ style }}</style>
</head>
<body>
<header>
  <h1>Employment Tribunal Decisions</h1>
  <nav>
    <a href="/">Search</a>
    <a href="/pinned" id="saved-nav-link">Saved</a>
  </nav>
</header>
<div class="container">
  <a class="back" href="javascript:history.back()">&#8592; Back to results</a>
  <div class="decision-meta">
    <h2>{{ d['title'] or 'Untitled' }}</h2>
    <div class="pin-detail">
      <button class="pin-btn" data-slug="{{ d['slug'] }}"
              onclick="togglePin(this,event)" title="Save for later">&#9734;</button>
      <span class="pin-label" style="font-size:0.85rem;color:#888;vertical-align:middle;">Save for later</span>
    </div>
    <table>
      <tr><td>Tribunal</td>
          <td>{{ 'Employment Appeal Tribunal' if d['tribunal_type']=='eat' else ('Court of Appeal (Civil)' if d['tribunal_type']=='ca' else 'Employment Tribunal') }}</td></tr>
      {% if d.get('neutral_citation') %}
      <tr><td>Citation</td><td><em>{{ d['neutral_citation'] }}</em></td></tr>
      {% endif %}
      <tr><td>Decision date</td><td>{{ d['decision_date'] or '—' }}</td></tr>
      <tr><td>Country</td><td>{{ (d['country'] or '—').replace('-',' ').title() }}</td></tr>
      {% if d['categories'] %}
      <tr><td>Categories</td>
          <td>{{ ', '.join(json.loads(d['categories'])) }}</td></tr>
      {% endif %}
      {% if d['pdf_url'] %}
      <tr><td>PDF</td>
          <td><a href="{{ d['pdf_url'] }}" target="_blank" rel="noopener">Download PDF ↗</a></td></tr>
      {% endif %}
      <tr><td>GOV.UK</td>
          <td><a href="https://www.gov.uk{{ d['slug'] }}" target="_blank" rel="noopener">
            www.gov.uk{{ d['slug'] }} ↗</a></td></tr>
    </table>
  </div>

  {% if d['full_text'] %}
  <div class="full-text">{{ d['full_text'] }}</div>
  {% else %}
  <p style="color:#888">Full text has not been fetched yet.</p>
  {% endif %}
</div>
{{ shared_js|safe }}
<script>
// Update the label next to the pin button to reflect current state
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.querySelector('.pin-detail .pin-btn');
  if (!btn) return;
  var label = document.querySelector('.pin-label');
  function sync() {
    var pinned = btn.classList.contains('pinned');
    if (label) label.textContent = pinned ? 'Saved' : 'Save for later';
  }
  window.afterPinToggle = function () { sync(); };
  sync();
});
</script>
</body>
</html>
"""

PINNED_TMPL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Saved Decisions — ET Decisions</title>
  <style>{{ style }}</style>
</head>
<body>
<header>
  <h1>Employment Tribunal Decisions</h1>
  <nav>
    <a href="/">Search</a>
    <a href="/pinned" class="active" id="saved-nav-link">Saved</a>
  </nav>
</header>
<div class="container">
  <div class="results-header" id="pin-header">Loading&hellip;</div>
  <div id="pin-results"></div>
</div>
{{ shared_js|safe }}
<script>
function esc(s) {
  var d = document.createElement('div');
  d.appendChild(document.createTextNode(String(s || '')));
  return d.innerHTML;
}
function titleCase(s) {
  return String(s || '').replace(/-/g, ' ').split(' ').map(function (w) {
    return w.charAt(0).toUpperCase() + w.slice(1);
  }).join(' ');
}

window.afterPinToggle = function (slug, nowPinned, btn) {
  if (!nowPinned) {
    var card = btn.closest('.result');
    if (card) {
      card.style.transition = 'opacity 0.2s';
      card.style.opacity = '0';
      setTimeout(function () {
        card.remove();
        var n = document.querySelectorAll('#pin-results .result').length;
        document.getElementById('pin-header').textContent =
          n === 0 ? 'No saved decisions.' : n + ' saved decision' + (n !== 1 ? 's' : '');
      }, 200);
    }
  }
};

document.addEventListener('DOMContentLoaded', function () {
  var pins = JSON.parse(localStorage.getItem('et_pins') || '[]');
  var header = document.getElementById('pin-header');
  var results = document.getElementById('pin-results');

  if (pins.length === 0) {
    header.textContent = 'No saved decisions.';
    return;
  }

  fetch('/api/pins?slugs=' + encodeURIComponent(pins.join(',')))
    .then(function (r) { return r.json(); })
    .then(function (rows) {
      header.textContent = rows.length + ' saved decision' + (rows.length !== 1 ? 's' : '');
      results.innerHTML = rows.map(function (r) {
        var cats = r.categories ? JSON.parse(r.categories) : [];
        var catHtml = cats.map(function (c) {
          return '<span class="tag cat">' + esc(titleCase(c)) + '</span>';
        }).join(' ');
        var typeLabel = r.tribunal_type === 'et' ? 'Employment Tribunal'
                      : r.tribunal_type === 'eat' ? 'Appeal Tribunal'
                      : r.tribunal_type === 'ca' ? 'Court of Appeal' : '';
        var typeTag = typeLabel
          ? '<span class="tag ' + esc(r.tribunal_type) + '">' + typeLabel + '</span>' : '';
        var country = r.country ? titleCase(r.country) : '';
        return '<div class="result">'
          + '<h2><a href="/decision' + esc(r.slug) + '">' + esc(r.title || r.slug) + '</a>'
          + '<button class="pin-btn pinned" data-slug="' + esc(r.slug) + '"'
          + ' onclick="togglePin(this,event)" title="Remove from saved">★</button></h2>'
          + '<div class="meta">'
          + (r.decision_date ? '<span>' + esc(r.decision_date) + '</span>' : '')
          + typeTag
          + (country ? '<span>' + esc(country) + '</span>' : '')
          + catHtml
          + '</div></div>';
      }).join('');
    });
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def search():
    q        = request.args.get("q", "").strip()
    tribunal = request.args.get("tribunal", "")
    country  = request.args.get("country", "")
    category = request.args.get("category", "")
    year     = request.args.get("year", "")
    outcome  = request.args.get("outcome", "")
    # Whitelist to prevent SQL injection via the column name in build_filters
    if outcome not in ("claimant_won", "respondent_won", "claimant_appealed", "respondent_appealed"):
        outcome = ""
    page     = max(1, int(request.args.get("page", 1) or 1))
    offset   = (page - 1) * PER_PAGE

    conn = open_db()

    db_total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    all_categories = [r[0] for r in conn.execute("""
        SELECT DISTINCT j.value FROM decisions, json_each(decisions.categories) j
        WHERE categories IS NOT NULL ORDER BY j.value
    """).fetchall()]

    all_years = [r[0] for r in conn.execute("""
        SELECT DISTINCT substr(decision_date, 1, 4) AS yr FROM decisions
        WHERE decision_date IS NOT NULL AND decision_date != ''
        ORDER BY yr DESC
    """).fetchall()]

    fts_ready = _fts_ready.is_set()

    if q and fts_ready:
        fq = to_fts_query(q)
        parts, params = build_filters(tribunal, country, category, year, outcome, prefix="d.")
        d_where = ("AND " + " AND ".join(parts)) if parts else ""
        try:
            rows = conn.execute(f"""
                SELECT d.slug, d.title, d.decision_date, d.tribunal_type, d.country,
                       d.categories, d.neutral_citation,
                       snippet(decisions_fts, 1, '<mark>', '</mark>', '&hellip;', 32) AS snippet
                FROM decisions_fts
                JOIN decisions d ON d.rowid = decisions_fts.rowid
                WHERE decisions_fts MATCH ? {d_where}
                ORDER BY rank
                LIMIT ? OFFSET ?
            """, [fq] + params + [PER_PAGE, offset]).fetchall()

            total = conn.execute(f"""
                SELECT COUNT(*) FROM decisions_fts
                JOIN decisions d ON d.rowid = decisions_fts.rowid
                WHERE decisions_fts MATCH ? {d_where}
            """, [fq] + params).fetchone()[0]
        except sqlite3.OperationalError:
            rows, total = [], 0
    else:
        parts, params = build_filters(tribunal, country, category, year, outcome)
        where = ("WHERE " + " AND ".join(parts)) if parts else ""
        rows = conn.execute(f"""
            SELECT slug, title, decision_date, tribunal_type, country, categories,
                   neutral_citation, NULL AS snippet
            FROM decisions {where}
            ORDER BY published_at DESC
            LIMIT ? OFFSET ?
        """, params + [PER_PAGE, offset]).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM decisions {where}", params
        ).fetchone()[0]

    conn.close()

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    return render_template_string(
        SEARCH_TMPL,
        style=STYLE, shared_js=SHARED_JS, results=rows, q=q,
        tribunal=tribunal, country=country,
        category=category, year=year, outcome=outcome,
        all_categories=all_categories, all_years=all_years,
        page=page, pages=pages, total=total, db_total=db_total,
        fts_ready=fts_ready, page_url=page_url,
        json=json, fmt_category=fmt_category,
    )


@app.route("/decision/<path:slug>")
def decision(slug):
    if not slug.startswith("/"):
        slug = "/" + slug
    conn = open_db()
    row = conn.execute("SELECT * FROM decisions WHERE slug=?", (slug,)).fetchone()
    conn.close()
    if row is None:
        abort(404)
    return render_template_string(
        DETAIL_TMPL, style=STYLE, shared_js=SHARED_JS, d=dict(row), json=json,
    )


@app.route("/pinned")
def pinned():
    return render_template_string(PINNED_TMPL, style=STYLE, shared_js=SHARED_JS)


@app.route("/api/pins")
def api_pins():
    slugs_param = request.args.get("slugs", "")
    slugs = [s.strip() for s in slugs_param.split(",") if s.strip()]
    if not slugs:
        return app.response_class(json.dumps([]), mimetype="application/json")
    conn = open_db()
    placeholders = ",".join("?" * len(slugs))
    rows = conn.execute(
        f"SELECT slug, title, decision_date, tribunal_type, country, categories "
        f"FROM decisions WHERE slug IN ({placeholders})",
        slugs,
    ).fetchall()
    conn.close()
    return app.response_class(
        json.dumps([dict(r) for r in rows]),
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting on http://0.0.0.0:{PORT}")
    print(f"Access from other devices at http://192.168.0.104:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
