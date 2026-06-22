#!/usr/bin/env python3
"""
NPS News & Press Daily Updater (GitHub Actions edition)
========================================================

Runs headlessly in CI. Calls the Anthropic API with the native web_search tool
to find new articles about NPS sign/exhibit censorship, then inserts HTML
cards into ``news-and-press.html`` in chronological order.

Environment:
    ANTHROPIC_API_KEY   Required. Your Anthropic API key.
    GITHUB_ACTIONS      Set by GitHub Actions; enables GH-specific output.

Usage:
    python scripts/update_news.py              # Normal run
    python scripts/update_news.py --dry-run    # Print diff; do not write
    python scripts/update_news.py --force      # Run even if banner already shows today
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_FILE = REPO_ROOT / "news-and-press.html"
ARCHIVE_DIR = REPO_ROOT / "News and Press" / "Archive"

# Keep in sync with the SKILL frontmatter. Fall back through a small list so
# the workflow does not break the first time Anthropic retires a model alias.
MODEL_CANDIDATES = [
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
]

MAX_TOOL_USES = 15
MAX_TOKENS = 8000

VALID_TAGS = {"order", "removal", "resistance", "lawsuit", "leak", "court"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("update-news")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class Article:
    """A curated article to add to the news page."""

    date: str                 # ISO format YYYY-MM-DD
    source_name: str          # Display name, e.g. "Washington Blade"
    source_key: str           # Filter key, e.g. "other"
    tag: str                  # One of VALID_TAGS
    tag_label: str            # Display label, e.g. "Court Victory"
    url: str
    headline: str
    summary_html: str         # Paraphrased summary; may contain <strong>

    @property
    def iso_month(self) -> str:
        return self.date[:7]

    @property
    def year(self) -> str:
        return self.date[:4]

    @property
    def display_month_day(self) -> str:
        dt = datetime.strptime(self.date, "%Y-%m-%d")
        return dt.strftime("%b %-d") if sys.platform != "win32" else dt.strftime("%b %#d")


def load_html() -> str:
    if not HTML_FILE.exists():
        log.error("HTML file not found: %s", HTML_FILE)
        sys.exit(1)
    return HTML_FILE.read_text(encoding="utf-8")


def write_html(content: str) -> None:
    HTML_FILE.write_text(content, encoding="utf-8")


_HOST_PREFIXES = ("www.", "us.", "uk.", "m.", "mobile.", "amp.")


def canonical_url(url: str) -> str:
    """Normalize a URL for dedup. Lowercases the host, strips common host
    prefixes (www., us., uk., m., mobile., amp.), drops query string and
    fragment, and trims a trailing slash. Catches near-duplicates like
    `www.cnn.com/X` vs `us.cnn.com/X` (same article, different mirror)."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url.strip())
        host = p.netloc.lower()
        for pref in _HOST_PREFIXES:
            if host.startswith(pref):
                host = host[len(pref):]
                break
        path = p.path.rstrip("/")
        return urlunparse((p.scheme.lower() or "https", host, path, "", "", ""))
    except Exception:
        return url.rstrip("/")


def existing_urls(html: str) -> set[str]:
    return {canonical_url(m) for m in re.findall(r'href="(https?://[^"]+)"', html)}


def banner_date(html: str) -> str | None:
    m = re.search(r'datetime="(\d{4}-\d{2}-\d{2})"', html)
    return m.group(1) if m else None


def article_count(html: str) -> int:
    return html.count('<article class="article-card"')


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------


CURATION_PROMPT = """You are the daily news curator for MissingParkHistory.org.

Your task: find NEW articles about NPS sign/exhibit censorship under Executive
Order 14253 and Secretary's Order 3431, and return them as strict JSON.

**Relevance**: only articles directly about one of:
- NPS sign or exhibit removal/censorship
- EO 14253 ("Restoring Truth and Sanity to American History")
- Secretary's Order 3431
- Related litigation (NPCA v. DOI, Philadelphia's President's House suit,
  Sierra Club FOIA suit, Stonewall Pride flag suit)
- Preservation / resistance efforts (Save Our Signs, MissingParkHistory.org,
  legislative responses, advocacy coalitions)
- Congressional action: bills, resolutions, floor statements, press releases,
  committee hearings, and appropriations language related to NPS censorship,
  national park funding cuts, or NPS staffing reductions
- NPS budget cuts, staffing freezes, visitor center closures, or fee-free day
  changes tied to the current administration
- Books, commentary, or op-eds about national park history censorship,
  Indigenous erasure in parks, or climate science removal from parks
- State-level resistance or co-management pushback against federal NPS
  censorship directives

**Novelty**: skip articles whose URLs appear in EXISTING_URLS below. Skip
articles that merely rehash prior coverage without new facts, quotes, or
developments.

**Source quality**: prioritize original reporting from major outlets (WaPo,
NYT, NPR, AP, PBS, CBS, CNN) and domain-specific sources (NPCA, Outside, NPS
Traveler, Democracy Forward, Sierra Club). Congressional press releases from
senate.gov and house.gov are high-priority primary sources. Regional reporting
and opinion/commentary are welcome when they add local context or substantive
analysis.

**Time window**: only articles published on or after {since_date}.

**Searches to run** (use the web_search tool — run ALL of these; cast a wide
net and vary your queries):

Core NPS censorship:
- "NPS sign removal" OR "national park censorship" {current_month_year}
- "SO 3431" OR "Secretary Order 3431" {current_month_year}
- national park exhibit removed signs {current_month_year}
- "national park" "history removed" OR "history censored" {current_year}

Litigation:
- NPCA Democracy Forward national parks lawsuit {current_year}
- "President's House" Philadelphia slavery exhibit {current_month_year}
- Stonewall Pride flag national monument {current_month_year}
- Sierra Club national park FOIA {current_year}

Congressional / legislative:
- site:senate.gov national park censorship {current_year}
- site:house.gov national park censorship OR "national park service" {current_year}
- Congress "national park" bill censorship OR funding cuts {current_month_year}
- "national park service" appropriations amendment {current_year}

Indigenous, climate, and themed coverage:
- "national park" Indigenous history censorship OR erasure {current_month_year}
- "national park" climate change signs removed {current_month_year}
- "national park" slavery history removed OR censored {current_month_year}
- national park books censored OR flagged {current_month_year}

Budget and staffing:
- "national park service" budget cuts staffing {current_month_year}
- "national park service" visitor center closed OR closure {current_month_year}
- NPS fee-free days changed OR eliminated {current_year}

Opinion and regional:
- "national park censorship" opinion OR commentary {current_month_year}
- "national park" history censorship California OR Pennsylvania OR Massachusetts {current_month_year}

Also search these specific domains for recent pieces:
npca.org, nationalparkstraveler.org, democracyforward.org, sierraclub.org,
outsideonline.com, calmatters.org, hcn.org (High Country News),
markey.senate.gov, merkley.senate.gov, grijalva.house.gov

**Filtering**: Do NOT include articles about:
- Big Tech censorship, Section 230, social media content moderation
- State or city parks (only National Park Service)
- General conservation topics unrelated to NPS interpretive censorship

**Output**: return ONLY valid JSON matching this schema. Your response MUST
begin with the `{{` character — no prose intro ("Based on my searches..."),
no preamble, no markdown fences, no commentary after the JSON. The very
first character of your output must be `{{` and the last must be `}}`.

{{
  "articles": [
    {{
      "date": "YYYY-MM-DD",
      "source_name": "Display name (e.g. 'Washington Blade')",
      "source_key": "one of: wapo|npr|nbc|pbs|newsweek|thehill|outside|sfgate|inquirer|bostonglobe|npca|demforward|sierraclub|oah|govexec|notus|calmatters|hcn|senate|house|congress|other",
      "tag": "one of: order|removal|resistance|lawsuit|leak|court",
      "tag_label": "Short display label (e.g. 'Court Victory', 'Removal', 'State Coalition')",
      "url": "https://...",
      "headline": "Article headline",
      "summary_html": "2-4 sentence paraphrased summary. Wrap key terms in <strong>...</strong>. NEVER quote the source article directly — paraphrase. Include specific names, dates, and numbers."
    }}
  ]
}}

If no qualifying new articles are found, return {{"articles": []}}.

---

EXISTING_URLS (do not duplicate):
{existing_urls_list}
"""


def call_claude(since_date: str, urls: Iterable[str]) -> list[Article]:
    client = anthropic.Anthropic()
    now = datetime.now()
    prompt = CURATION_PROMPT.format(
        since_date=since_date,
        current_month_year=now.strftime("%B %Y"),
        current_year=now.year,
        existing_urls_list="\n".join(sorted(urls)),
    )

    def _create(model: str, max_uses: int):
        log.info("Calling Anthropic API with model=%s (max_uses=%d)", model, max_uses)
        return client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }],
            messages=[{"role": "user", "content": prompt}],
        )

    last_err: Exception | None = None
    resp = None
    for model in MODEL_CANDIDATES:
        try:
            resp = _create(model, MAX_TOOL_USES)
            break
        except anthropic.NotFoundError as e:
            log.warning("Model %s not available: %s", model, e)
            last_err = e
            continue
        except anthropic.BadRequestError as e:
            if "prompt is too long" not in str(e):
                raise
            retry_uses = max(1, MAX_TOOL_USES // 2)
            log.warning(
                "Prompt too long with max_uses=%d; retrying once with max_uses=%d",
                MAX_TOOL_USES, retry_uses,
            )
            try:
                resp = _create(model, retry_uses)
                break
            except anthropic.BadRequestError as e2:
                log.error("Prompt still too long on retry; aborting run: %s", e2)
                emit_github_summary([
                    "## NPS News Daily Update",
                    "- Status: **Failed** — Anthropic API rejected the prompt as too long even after halving max_uses.",
                    "- Action: lower `MAX_TOOL_USES` further or trim queries in `CURATION_PROMPT`.",
                ])
                sys.exit(1)
    else:
        log.error("All candidate models failed; last error: %s", last_err)
        sys.exit(1)

    # Claude responses with tools include a mix of text and tool_use/tool_result
    # blocks. We want the final assistant text block.
    final_text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            final_text = block.text  # keep last text block

    if not final_text:
        log.warning("No text block in Claude response")
        return []

    # Strip common junk
    final_text = final_text.strip()
    final_text = re.sub(r"^```(?:json)?\s*", "", final_text)
    final_text = re.sub(r"\s*```$", "", final_text)

    # Claude sometimes prefaces the JSON with prose ("Based on my searches...").
    # Extract the outermost JSON object — first '{' to last '}' — before parsing.
    first_brace = final_text.find("{")
    last_brace = final_text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        final_text = final_text[first_brace : last_brace + 1]

    try:
        data = json.loads(final_text)
    except json.JSONDecodeError as e:
        log.error("Claude returned non-JSON output: %s", e)
        log.error("Raw output (first 1000 chars): %s", final_text[:1000])
        return []

    parsed = [parse_article(a) for a in data.get("articles", []) if a]
    return [a for a in parsed if a is not None]


def parse_article(raw: dict) -> Article | None:
    # A single malformed article from the model must not abort the whole run.
    # Skip (with a warning) any entry missing a required field.
    required = ("date", "source_name", "url", "headline", "summary_html")
    missing = [k for k in required if not str(raw.get(k, "")).strip()]
    if missing:
        log.warning("Skipping malformed article (missing %s): %r", ", ".join(missing), raw)
        return None

    tag = raw.get("tag", "").strip().lower()
    if tag not in VALID_TAGS:
        log.warning("Invalid tag %r; coercing to 'removal'", tag)
        tag = "removal"
    return Article(
        date=raw["date"].strip(),
        source_name=raw["source_name"].strip(),
        source_key=raw.get("source_key", "other").strip().lower() or "other",
        tag=tag,
        tag_label=raw.get("tag_label", tag.title()).strip() or tag.title(),
        url=raw["url"].strip(),
        headline=raw["headline"].strip(),
        summary_html=raw["summary_html"].strip(),
    )


# ---------------------------------------------------------------------------
# HTML generation & insertion
# ---------------------------------------------------------------------------


def render_card(a: Article) -> str:
    return (
        f'  <article class="article-card" data-tags="{a.tag}" '
        f'data-month="{a.iso_month}" data-source="{a.source_key}">\n'
        f'    <div class="article-date">'
        f'<div class="month-day">{a.display_month_day}</div>'
        f'<div class="date-detail">{a.year}</div></div>\n'
        f'    <div class="article-body">\n'
        f'      <div class="article-meta">'
        f'<span class="article-source">{a.source_name}</span>'
        f'<span class="article-tag tag-{a.tag}">{a.tag_label}</span></div>\n'
        f'      <h3><a href="{a.url}" target="_blank">{a.headline}</a></h3>\n'
        f'      <p class="article-summary">{a.summary_html}</p>\n'
        f'      <a href="{a.url}" class="read-more" target="_blank">'
        f'Read at {a.source_name} &rarr;</a>\n'
        f'    </div>\n'
        f'  </article>\n'
    )


def insert_card(html: str, card: str, year: str) -> str:
    """Insert a card at the top of the given year's section.

    The page is ordered newest-first within each year. We insert right after the
    ``<div class="year-marker"><h2>{year}</h2></div>`` line.
    """
    marker = f'<div class="year-marker"><h2>{year}</h2></div>'
    idx = html.find(marker)
    if idx == -1:
        # Create a new year section at the top of the timeline section.
        section_open = '<section class="timeline-section">'
        sec_idx = html.find(section_open)
        if sec_idx == -1:
            log.warning("Could not locate timeline section; skipping insert")
            return html
        insert_at = sec_idx + len(section_open)
        return (
            html[:insert_at]
            + f'\n<div class="year-marker"><h2>{year}</h2></div>\n'
            + card
            + html[insert_at:]
        )

    # Insert immediately after the marker line (after its closing newline).
    after_marker = html.find("\n", idx) + 1
    return html[:after_marker] + card + html[after_marker:]


def sort_articles_desc(articles: list[Article]) -> list[Article]:
    return sorted(articles, key=lambda a: a.date, reverse=True)


# ---------------------------------------------------------------------------
# Banner update
# ---------------------------------------------------------------------------


def update_banner(html: str, today_iso: str, new_count: int) -> str:
    dt = datetime.strptime(today_iso, "%Y-%m-%d")
    pretty = dt.strftime("%B %-d, %Y") if sys.platform != "win32" else dt.strftime("%B %#d, %Y")

    html = re.sub(
        r'datetime="\d{4}-\d{2}-\d{2}">[^<]+<',
        f'datetime="{today_iso}">{pretty}<',
        html,
        count=1,
    )
    html = re.sub(
        r"(\d+)\s+articles tracked",
        f"{new_count} articles tracked",
        html,
        count=1,
    )
    return html


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


INDEX_FILE = REPO_ROOT / "index.html"

TAG_LABELS = {
    "order": "The Order",
    "removal": "Removals",
    "resistance": "Resistance",
    "lawsuit": "Lawsuit",
    "leak": "Leaks",
    "court": "Court Victory",
}


def _recent_articles_from_html(html: str, limit: int = 5) -> list[dict]:
    """Extract the N most recent articles from news-and-press.html for the
    digest pop-up.  Returns dicts with keys: day, month, source, tag,
    tag_label, headline, url."""
    pattern = re.compile(
        r'<article class="article-card"[^>]*data-tags="([^"]*)"[^>]*>'
        r'.*?<div class="month-day">([^<]+)</div>'
        r'.*?<div class="date-detail">([^<]+)</div>'
        r'.*?<span class="article-source">([^<]+)</span>'
        r'.*?<h3><a href="([^"]+)"[^>]*>([^<]+)</a></h3>',
        re.DOTALL,
    )
    results = []
    for m in pattern.finditer(html):
        tag = m.group(1).strip()
        month_day = m.group(2).strip()          # e.g. "Apr 20"
        year = m.group(3).strip()               # e.g. "2026"
        parts = month_day.split()
        mon = parts[0] if parts else ""
        day = parts[1] if len(parts) > 1 else ""
        results.append({
            "day": day,
            "mon": mon,
            "year": year,
            "source": m.group(4).strip(),
            "tag": tag,
            "tag_label": TAG_LABELS.get(tag, tag.title()),
            "url": m.group(5).strip(),
            "headline": m.group(6).strip(),
        })
        if len(results) >= limit:
            break
    return results


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _articles_for_synthesis(html: str, since_date: str, limit: int = 20) -> list[dict]:
    """Extract articles published on or after since_date for the weekly
    synthesis prompt. Returns dicts with date, source, tag, headline, url,
    summary (HTML, may contain <strong>/<em>)."""
    cards = re.findall(
        r'<article class="article-card"[^>]*>.*?</article>', html, re.DOTALL,
    )
    sy, sm, sd = (int(x) for x in since_date.split("-"))
    results = []
    for c in cards:
        md = re.search(r'<div class="month-day">([^<]+)</div>', c)
        yr = re.search(r'<div class="date-detail">([^<]+)</div>', c)
        src = re.search(r'<span class="article-source">([^<]+)</span>', c)
        h = re.search(r'<h3[^>]*>(?:.*?<a[^>]*>)?([^<]+)<', c, re.DOTALL)
        url = re.search(r'<h3[^>]*>.*?<a href="([^"]+)"', c, re.DOTALL)
        summary = re.search(r'<p class="article-summary">(.+?)</p>', c, re.DOTALL)
        tag = re.search(r'data-tags="([^"]*)"', c)
        if not (md and yr and src and h):
            continue
        tok = md.group(1).strip().split()
        if len(tok) < 2 or not tok[1].isdigit():
            continue
        month = _MONTHS.get(tok[0][:3])
        day = int(tok[1])
        ym = re.search(r'(\d{4})', yr.group(1))
        year = int(ym.group(1)) if ym else 0
        if not (month and year):
            continue
        if (year, month, day) < (sy, sm, sd):
            continue
        results.append({
            "date": f"{year:04d}-{month:02d}-{day:02d}",
            "source": src.group(1).strip(),
            "tag": tag.group(1) if tag else "",
            "headline": h.group(1).strip(),
            "url": url.group(1) if url else "",
            "summary": re.sub(r"\s+", " ", summary.group(1)).strip() if summary else "",
        })
        if len(results) >= limit:
            break
    return results


def _tag_css_class(tag: str) -> str:
    return f"nd-tag-{tag}" if tag in TAG_LABELS else "nd-tag-order"


def update_news_digest(news_html: str, today_iso: str) -> None:
    """Regenerate the news digest pop-up inside index.html using the latest
    articles from the news page."""
    if not INDEX_FILE.exists():
        log.warning("index.html not found; skipping digest update")
        return

    articles = _recent_articles_from_html(news_html, limit=5)
    if not articles:
        log.info("No articles found for digest pop-up; skipping")
        return

    idx_html = INDEX_FILE.read_text(encoding="utf-8")

    # Build the articles block
    art_lines = []
    for a in articles:
        art_lines.append(
            f'      <div class="nd-article">\n'
            f'        <div class="nd-art-date"><div class="nd-day">{a["day"]}</div>'
            f'<div class="nd-mon">{a["mon"]}</div></div>\n'
            f'        <div class="nd-art-body">\n'
            f'          <div class="nd-art-meta"><span class="nd-art-source">{a["source"]}</span>'
            f'<span class="nd-art-tag {_tag_css_class(a["tag"])}">{a["tag_label"]}</span></div>\n'
            f'          <div class="nd-art-title"><a href="{a["url"]}" target="_blank" '
            f'rel="noopener">{a["headline"]}</a></div>\n'
            f'        </div>\n'
            f'      </div>\n'
        )
    articles_block = "\n".join(art_lines)

    # Build date range string
    if articles:
        first = articles[-1]
        last_art = articles[0]
        date_range = f'{first["mon"]} {first["day"]} &ndash; {last_art["mon"]} {last_art["day"]}, {last_art["year"]}'
    else:
        date_range = today_iso

    # Replace the articles section between markers
    start_marker = '<div class="nd-articles">'
    end_marker = '</div>\n\n    <div class="nd-footer">'

    start_idx = idx_html.find(start_marker)
    end_idx = idx_html.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        log.warning("Could not find digest markers in index.html; skipping")
        return

    new_articles_section = (
        f'<div class="nd-articles">\n'
        f'      <h3>Latest Coverage</h3>\n\n'
        f'{articles_block}'
        f'    '
    )

    new_idx = idx_html[:start_idx] + new_articles_section + idx_html[end_idx:]

    # Update date range in header
    new_idx = re.sub(
        r'(<div class="nd-daterange">)[^<]+(</div>)',
        rf'\g<1>{date_range} &middot; {len(articles)} new developments\2',
        new_idx,
        count=1,
    )

    INDEX_FILE.write_text(new_idx, encoding="utf-8")
    log.info("Updated news digest pop-up in index.html with %d articles", len(articles))


SYNTHESIS_PROMPT = """You are the editorial voice of MissingParkHistory.org,
regenerating the "Where the Fight Stands" synthesis at the top of the
homepage news digest. Your output replaces a hand-curated narrative.

Tone reference (the most recent prior version — match this voice exactly):
---
{prior_synthesis}
---

Rules:
- TWO paragraphs, ~150-200 words each. No more, no less.
- Lead with the WEEK'S ANCHOR STORY (the single most consequential
  development), then add a second paragraph that adds another thread
  (legal, executive, or congressional) and contextualizes how the pieces
  fit together. Find the throughline; do not summarize each article.
- Voice: analytical, journalistic, structural. Use specific names, dates,
  numbers, dollar amounts. No vague phrases like "this week saw" or "many
  developments occurred."
- HTML: wrap key proper nouns and figures in <strong>...</strong>.
  Italicize key phrases, titles, or quoted concepts in <em>...</em>.
  Use &mdash; for em-dashes and &rsquo; for apostrophes.

ALSO produce THREE badge labels for the header. Each is one emoji + a short
label (3-5 words). Pick the three most newsworthy threads. Use one of
these unicode emojis (NOT HTML entities):
- 🏛️ for legislative/congressional action
- ⚖️ for courts/lawsuits
- 👁️ for removals/censorship visibility
- ✊ for resistance/protest
- 📖 for books/publications
- 📰 for media/press
- 📜 for executive orders / orders
- 🍁 for Indigenous/Native American themes

ARTICLES from {since_date} through {today} (most recent first):
{articles_block}

Return ONLY this JSON. The very first character of your response MUST be
`{{` — no prose intro, no markdown fences, no commentary:

{{
  "paragraphs": [
    "first paragraph HTML, with <strong> and <em>",
    "second paragraph HTML"
  ],
  "badges": [
    {{"emoji": "🏛️", "label": "Truth in NPs Act Introduced"}},
    {{"emoji": "⚖️", "label": "Plaintiffs File New Brief"}},
    {{"emoji": "📖", "label": "Native History in Spotlight"}}
  ]
}}
"""


def update_synthesis(news_html: str, today_iso: str) -> bool:
    """Regenerate the 'Where the Fight Stands' synthesis paragraphs and the
    three header badges in index.html, using Claude over the last 7 days of
    articles. Returns True on success, False on any soft failure (caller
    should treat False as "leave existing synthesis in place")."""
    if not INDEX_FILE.exists():
        log.warning("index.html not found; skipping synthesis update")
        return False

    since = datetime.now().date().toordinal() - 7
    since_date = datetime.fromordinal(since).strftime("%Y-%m-%d")
    articles = _articles_for_synthesis(news_html, since_date, limit=20)
    if len(articles) < 2:
        log.warning("Only %d article(s) in past 7 days; skipping synthesis", len(articles))
        return False

    idx_html = INDEX_FILE.read_text(encoding="utf-8")
    syn_re = re.compile(
        r'<div class="nd-synthesis">.*?</div>(\s*\n\s*<div class="nd-articles">)',
        re.DOTALL,
    )
    badge_re = re.compile(
        r'<div class="nd-badge-row">.*?</div>(\s*\n\s*</div>)',
        re.DOTALL,
    )
    syn_match = syn_re.search(idx_html)
    badge_match = badge_re.search(idx_html)
    if not (syn_match and badge_match):
        log.warning("Could not locate synthesis or badge-row markers; skipping")
        return False

    prior_synthesis = syn_match.group(0).split('<div class="nd-articles">')[0].strip()
    articles_block = "\n\n".join(
        f"[{a['date']}] {a['source']} ({a['tag']}) — {a['headline']}\n"
        f"  Summary: {a['summary'][:500]}\n"
        f"  URL: {a['url']}"
        for a in articles
    )

    prompt = SYNTHESIS_PROMPT.format(
        prior_synthesis=prior_synthesis,
        since_date=since_date,
        today=today_iso,
        articles_block=articles_block,
    )

    client = anthropic.Anthropic()
    resp = None
    last_err: Exception | None = None
    for model in MODEL_CANDIDATES:
        try:
            log.info("Calling Anthropic API for synthesis with model=%s", model)
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except anthropic.NotFoundError as e:
            log.warning("Model %s not available: %s", model, e)
            last_err = e
            continue
        except anthropic.AnthropicError as e:
            log.error("Synthesis API call failed: %s", e)
            return False
    if resp is None:
        log.error("All candidate models failed for synthesis; last error: %s", last_err)
        return False

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text
    if not text:
        log.warning("No text in synthesis response")
        return False

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    fb = text.find("{")
    lb = text.rfind("}")
    if fb != -1 and lb > fb:
        text = text[fb : lb + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Synthesis returned non-JSON: %s", e)
        log.error("Raw (first 1000 chars): %s", text[:1000])
        return False

    paragraphs = data.get("paragraphs", [])
    badges = data.get("badges", [])
    if len(paragraphs) != 2 or not all(isinstance(p, str) and len(p) > 50 for p in paragraphs):
        log.warning("Synthesis paragraph schema mismatch (expected 2 substantive strings)")
        return False
    if len(badges) != 3 or not all(isinstance(b, dict) and "emoji" in b and "label" in b for b in badges):
        log.warning("Synthesis badge schema mismatch (expected 3 dicts with emoji+label)")
        return False

    new_synthesis = (
        '<div class="nd-synthesis">\n'
        '      <h3>Where the Fight Stands</h3>\n'
        f'      <p>{paragraphs[0]}</p>\n'
        f'      <p>{paragraphs[1]}</p>\n'
        '    </div>'
    )
    badge_lines = "\n".join(
        f'        <span class="nd-badge">{b["emoji"]} {b["label"]}</span>'
        for b in badges
    )
    new_badges = (
        '<div class="nd-badge-row">\n'
        f'{badge_lines}\n'
        '      </div>'
    )

    new_idx = syn_re.sub(new_synthesis + r"\1", idx_html, count=1)
    new_idx = badge_re.sub(new_badges + r"\1", new_idx, count=1)

    INDEX_FILE.write_text(new_idx, encoding="utf-8")
    log.info(
        "Updated synthesis: %d paragraphs (~%d words), %d badges",
        len(paragraphs),
        sum(len(re.sub(r"<[^>]+>", "", p).split()) for p in paragraphs),
        len(badges),
    )
    return True


def archive(today_iso: str) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVE_DIR / f"news-and-press_{today_iso.replace('-', '')}.html"
    shutil.copy2(HTML_FILE, dst)
    log.info("Archived previous version to %s", dst.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def emit_github_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="NPS news daily updater")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Run even if banner already shows today")
    parser.add_argument(
        "--force-synthesis",
        action="store_true",
        help="Refresh the 'Where the Fight Stands' synthesis even on non-Mondays",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set")
        return 2

    html = load_html()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    last = banner_date(html)
    log.info("Banner last-updated: %s | today: %s", last, today_iso)

    # Synthesis refresh runs weekly on Mondays (or whenever --force-synthesis is set).
    # weekday(): Monday is 0.
    is_monday = datetime.now().weekday() == 0
    do_synthesis = (is_monday or args.force_synthesis) and not args.dry_run

    if last == today_iso and not args.force:
        log.info("Banner already shows today; nothing to do for articles.")
        if do_synthesis:
            log.info("Refreshing weekly synthesis narrative.")
            update_synthesis(html, today_iso)
        emit_github_summary([
            "## NPS News Daily Update",
            f"- Date: **{today_iso}**",
            "- Status: **Skipped articles** — banner already shows today.",
            f"- Synthesis refreshed: **{do_synthesis}**",
        ])
        return 0

    # Call Claude
    # Search for articles from the last 21 days so we catch anything missed,
    # including articles that are slow to appear in search indexes.
    since = (datetime.now().date().toordinal() - 21)
    since_date = datetime.fromordinal(since).strftime("%Y-%m-%d")
    existing = existing_urls(html)
    articles = call_claude(since_date, existing)
    # Dedup any returned-but-already-present URLs, AND any duplicates within
    # the same response (e.g. canonical + mirror of one CNN article).
    seen: set[str] = set()
    deduped = []
    for a in articles:
        c = canonical_url(a.url)
        if c in existing or c in seen:
            continue
        seen.add(c)
        deduped.append(a)
    articles = sort_articles_desc(deduped)
    log.info("Claude returned %d qualifying new article(s)", len(articles))

    if not articles:
        # Bump banner date only.
        new_html = update_banner(html, today_iso, article_count(html))
        if args.dry_run:
            log.info("[dry-run] Would bump banner date only.")
        else:
            archive(today_iso)
            write_html(new_html)
            update_news_digest(new_html, today_iso)
            if do_synthesis:
                log.info("Refreshing weekly synthesis narrative.")
                update_synthesis(new_html, today_iso)
            log.info("No new articles. Banner date bumped.")
        emit_github_summary([
            "## NPS News Daily Update",
            f"- Date: **{today_iso}**",
            "- Status: **No new articles**. Banner date updated.",
            f"- Synthesis refreshed: **{do_synthesis}**",
        ])
        return 0

    # Insert newest-last so each insertion still lands at the top of the year.
    new_html = html
    for a in reversed(articles):
        card = render_card(a)
        new_html = insert_card(new_html, card, a.year)

    new_html = update_banner(new_html, today_iso, article_count(new_html))

    if args.dry_run:
        log.info("[dry-run] Would add %d article(s):", len(articles))
        for a in articles:
            log.info("  %s — %s (%s)", a.date, a.headline, a.url)
        return 0

    archive(today_iso)
    write_html(new_html)
    update_news_digest(new_html, today_iso)
    if do_synthesis:
        log.info("Refreshing weekly synthesis narrative.")
        update_synthesis(new_html, today_iso)
    log.info("Wrote %s with %d new article(s)", HTML_FILE.name, len(articles))

    lines = [
        "## NPS News Daily Update",
        f"- Date: **{today_iso}**",
        f"- **{len(articles)} new article(s) added**",
        f"- Synthesis refreshed: **{do_synthesis}**",
        "",
    ]
    for a in articles:
        lines.append(f"- **{a.date}** — [{a.source_name}]({a.url}) — {a.headline}")
    emit_github_summary(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
