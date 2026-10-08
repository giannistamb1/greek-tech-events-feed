#!/usr/bin/env python3
"""
Greek Tech Events — one RSS feed built from many sources.

Source types (set per source in sources.yaml):
  rss     classic RSS/Atom feed (news sites, Substack, Google Alerts, RSS.app bridges)
  ical    .ics calendar (Meetup groups, Luma calendars, Google Calendars)
  jsonld  an HTML listing page that embeds schema.org Event data
          (Eventbrite, Meetup search, AllEvents, 10Times, Luma city pages...)

Filters (per source):
  filter: none | tech | tech_event
      tech        keep items that mention a tech keyword
      tech_event  keep items that mention a tech keyword AND an event keyword
                  (use for news sites, so only event announcements get through)
  require_greece: true   keep only items that mention a Greek place (for EU-wide sources)

Usage:
  python build_feed.py             build docs/feed.xml (+ feed-events.xml, feed-news.xml, feed.json)
  python build_feed.py --check     test every source and print a health table, write nothing
  python build_feed.py --only "Startupper"   run a single source (combine with --check)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from icalendar import Calendar

ROOT = Path(__file__).resolve().parent
ATHENS = ZoneInfo("Europe/Athens")
NOW = datetime.now(timezone.utc)
log = logging.getLogger("feed")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GreekTechEventsFeed/1.0"),
    "Accept-Language": "en-US,en;q=0.8,el;q=0.7",
})

# Characters that are not allowed in XML 1.0 or cannot be encoded as UTF-8.
CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f" + chr(0xD800) + "-" + chr(0xDFFF)
                           + chr(0xFFFE) + chr(0xFFFF) + "]")
UNSAFE_IN_URL = re.compile(r"[\s\x00-\x1f\x7f<>\"'`\\]")
MAX_URL_LENGTH = 2000
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "aff", "ref", "_hs")


# ---------------------------------------------------------------- helpers
def norm(text: str) -> str:
    """Lowercase, strip accents (Greek tonos too), unify final sigma."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower().replace("ς", "σ")


def compile_patterns(words: list[str]) -> list[re.Pattern]:
    return [re.compile(norm(w)) for w in words]


def matches(text: str, patterns: list[re.Pattern]) -> bool:
    t = norm(text)
    return any(p.search(t) for p in patterns)


def clean_text(value, limit: int = 700) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    text = CONTROL_CHARS.sub("", re.sub(r"\s+", " ", text))
    return text[:limit] + ("…" if len(text) > limit else "")


def to_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, time.struct_time):
            d = datetime(*value[:6], tzinfo=timezone.utc)
        elif isinstance(value, datetime):
            d = value
        elif isinstance(value, date):
            d = datetime(value.year, value.month, value.day)
        else:
            d = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=ATHENS)
    return d


def clean_link(link) -> str:
    """Return the link exactly as it will be stored, or "" if it is not a plain http(s) URL.

    Validation and storage use the same string, so nothing that fails the check
    (javascript:, data:, whitespace, control characters, markup) can reach the feed.
    """
    link = str(link or "").strip()
    if not link or len(link) > MAX_URL_LENGTH or UNSAFE_IN_URL.search(link):
        return ""
    if CONTROL_CHARS.search(link):
        return ""
    try:
        parts = urlsplit(link)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return link


def is_web_link(link) -> bool:
    return bool(clean_link(link))


def fetch(url: str, settings: dict) -> tuple[bytes, str]:
    """GET a source with a size cap and a total time budget. Returns (body, final URL)."""
    if not is_web_link(url):
        raise ValueError("source url must be http(s)")
    max_bytes = settings.get("max_bytes", 5_000_000)
    deadline = time.monotonic() + settings.get("max_fetch_seconds", 60)
    with SESSION.get(url, timeout=settings.get("timeout", 30), stream=True) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(65536):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"response larger than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError("source took too long to send its response")
            chunks.append(chunk)
        return b"".join(chunks), resp.url


def canonical_url(link: str) -> str:
    parts = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith(TRACKING_PARAMS)]
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit((parts.scheme.lower() or "https", host,
                       parts.path.rstrip("/"), urlencode(query), ""))


# ---------------------------------------------------------------- model
@dataclass
class Item:
    title: str
    link: str
    source: str
    source_url: str
    category: str
    kind: str                       # "events" or "news"
    summary: str = ""
    location: str = ""
    uid: str = ""
    published: datetime | None = None
    starts: datetime | None = None
    first_seen: datetime = field(default_factory=lambda: NOW)

    @property
    def key(self) -> str:
        if self.uid:
            return "uid:" + self.uid
        return "url:" + canonical_url(self.link)

    @property
    def title_key(self) -> str:
        base = re.sub(r"[^\w]+", "", norm(self.title))[:80]
        day = self.starts.astimezone(ATHENS).date().isoformat() if self.starts else ""
        return f"{base}|{day}"


# ---------------------------------------------------------------- parsers
def parse_rss(src: dict, body: bytes, url: str):
    fp = feedparser.parse(body)
    if fp.bozo and not fp.entries:
        raise ValueError(f"not a valid feed ({fp.bozo_exception})")
    for e in fp.entries:
        yield dict(
            title=clean_text(e.get("title"), 300),
            link=e.get("link") or "",
            summary=clean_text(e.get("summary") or e.get("description")),
            published=to_dt(e.get("published_parsed") or e.get("updated_parsed")),
        )


def parse_ical(src: dict, body: bytes, url: str):
    cal = Calendar.from_ical(body)
    for ev in cal.walk("VEVENT"):
        description = str(ev.get("DESCRIPTION", ""))
        link = str(ev.get("URL") or "")
        if not link:
            found = URL_IN_TEXT.search(description)
            link = found.group(0) if found else src["url"]
        yield dict(
            title=clean_text(ev.get("SUMMARY"), 300),
            link=link,
            summary=clean_text(description),
            location=clean_text(ev.get("LOCATION"), 200),
            uid=str(ev.get("UID", "")),
            starts=to_dt(ev.decoded("DTSTART")) if ev.get("DTSTART") else None,
        )


def _walk(node):
    """Yield every dict in a JSON-LD tree; stop descending once an Event is found."""
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        if _is_event(node):
            yield node
            return
        for v in node.values():
            yield from _walk(v)


def _is_event(obj: dict) -> bool:
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.endswith("Event") for x in types)


def _location(loc) -> str:
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return loc
    if not isinstance(loc, dict):
        return ""
    if loc.get("@type") == "VirtualLocation":
        return "Online"
    parts = [loc.get("name", "")]
    addr = loc.get("address")
    if isinstance(addr, dict):
        parts += [addr.get("streetAddress", ""), addr.get("addressLocality", "")]
    elif isinstance(addr, str):
        parts.append(addr)
    return ", ".join(p for p in parts if p)


def parse_jsonld(src: dict, body: bytes, url: str):
    soup = BeautifulSoup(body, "html.parser")
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            blocks.append(json.loads(tag.string or tag.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
    events = list(_walk(blocks))
    if not events:
        raise ValueError("no schema.org Event data found on page (site may need a "
                         "different URL, or blocks scrapers)")
    for ev in events:
        yield dict(
            title=clean_text(ev.get("name"), 300),
            link=urljoin(url, str(ev.get("url") or "")) or src["url"],
            summary=clean_text(ev.get("description")),
            location=clean_text(_location(ev.get("location")), 200),
            starts=to_dt(ev.get("startDate")),
        )


PARSERS = {"rss": parse_rss, "ical": parse_ical, "jsonld": parse_jsonld}


# ---------------------------------------------------------------- pipeline
@dataclass
class Result:
    name: str
    fetched: int = 0
    kept: int = 0
    error: str = ""
    items: list = field(default_factory=list)


def run_source(src: dict, pats: dict, settings: dict) -> Result:
    res = Result(str(src.get("name") or src.get("url") or "unnamed source"))
    try:  # one broken source never breaks the feed
        res.items = collect(src, pats, settings, res)
    except Exception as exc:
        res.items = []
        res.error = f"{type(exc).__name__}: {exc}"[:300]
    res.kept = len(res.items)
    return res


def collect(src: dict, pats: dict, settings: dict, res: Result) -> list[Item]:
    body, final_url = fetch(src["url"], settings)
    raw = list(PARSERS[src.get("type", "rss")](src, body, final_url))
    res.fetched = len(raw)
    mode = src.get("filter", "tech")
    kind = src.get("kind", "events")
    max_news_age = timedelta(days=settings.get("max_news_age_days", 45))
    past_grace = timedelta(days=1)

    items = []
    for r in raw:
        r["link"] = clean_link(r.get("link"))
        if r.get("uid"):
            r["uid"] = CONTROL_CHARS.sub("", str(r["uid"]))[:300]
        if not r.get("title") or not r["link"]:
            continue
        text = " ".join([r["title"], r.get("summary", ""), r.get("location", "")])
        if mode in ("tech", "tech_event") and not matches(text, pats["tech"]):
            continue
        if mode == "tech_event" and not matches(text, pats["event"]):
            continue
        if src.get("require_greece") and not matches(text, pats["greece"]):
            continue
        starts, published = r.get("starts"), r.get("published")
        if starts and starts < NOW - past_grace:
            continue
        if kind == "news" and published and published < NOW - max_news_age:
            continue
        items.append(Item(source=src["name"], source_url=src.get("home", src["url"]),
                          category=src.get("category", "General"), kind=kind, **r))
    return items


def run_all(sources: list[dict], pats: dict, settings: dict) -> list[Result]:
    """Run every source in its own daemon thread under one hard wall-clock limit.

    The limit is enforced here, not inside the worker, so a source that stalls
    mid-read or hangs while parsing is reported as an error and left behind.
    """
    limit = settings.get("max_fetch_seconds", 60) + settings.get("timeout", 30)
    lock = threading.Lock()
    results: list[Result | None] = [None] * len(sources)
    state = {"closed": False}  # set once the limit has passed; late workers are ignored

    def work(i: int, src: dict):
        res = run_source(src, pats, settings)
        with lock:
            if not state["closed"]:
                results[i] = res

    threads = [threading.Thread(target=work, args=(i, s), daemon=True) for i, s in enumerate(sources)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + limit
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        state["closed"] = True
        done = list(results)
    return [res or Result(str(src.get("name") or "unnamed source"),
                          error=f"TimeoutError: no result within {limit} seconds")
            for res, src in zip(done, sources)]


def dedupe(items: list[Item]) -> list[Item]:
    # Prefer items that carry an event date, then items from event platforms.
    items.sort(key=lambda i: (i.starts is None, i.kind != "events"))
    seen_keys, seen_titles, out = set(), set(), []
    for it in items:
        if it.key in seen_keys or it.title_key in seen_titles:
            continue
        seen_keys.add(it.key)
        seen_titles.add(it.title_key)
        out.append(it)
    return out


def apply_first_seen(items: list[Item], state_path: Path, keep_days: int = 150):
    """Give each item a stable pubDate (first time we saw it) so readers don't reshuffle."""
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    for it in items:
        if it.key in state:
            it.first_seen = to_dt(state[it.key]) or NOW
        else:
            it.first_seen = it.published if it.published and it.published < NOW else NOW
            state[it.key] = it.first_seen.isoformat()
    cutoff = NOW - timedelta(days=keep_days)
    state = {k: v for k, v in state.items() if (to_dt(v) or NOW) >= cutoff}
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True))


# ---------------------------------------------------------------- output
def item_html(it: Item) -> str:
    rows = []
    if it.starts:
        rows.append(f"<b>When:</b> {it.starts.astimezone(ATHENS):%A %d %B %Y, %H:%M}")
    if it.location:
        rows.append(f"<b>Where:</b> {escape(it.location)}")
    rows.append(f"<b>Source:</b> {escape(it.source)} · {escape(it.category)}")
    body = f"<p>{'<br>'.join(rows)}</p>"
    if it.summary:
        body += f"<p>{escape(it.summary)}</p>"
    return body


def item_title(it: Item) -> str:
    if it.starts:
        return f"[{it.starts.astimezone(ATHENS):%d %b}] {it.title}"
    return it.title


def write_rss(items: list[Item], meta: dict, path: Path, subtitle: str = ""):
    attr = {'"': "&quot;"}
    title = meta["title"] + (f" — {subtitle}" if subtitle else "")
    self_url = meta["link"].rsplit("/", 1)[0] + "/" + path.name
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(meta.get('home', self_url))}</link>",
        f'<atom:link href="{escape(self_url, attr)}" rel="self" type="application/rss+xml"/>',
        f"<description>{escape(meta['description'])}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(NOW)}</lastBuildDate>",
        "<ttl>180</ttl>",
    ]
    for it in items:
        out += [
            "<item>",
            f"<title>{escape(CONTROL_CHARS.sub('', item_title(it)))}</title>",
            f"<link>{escape(it.link)}</link>",
            f'<guid isPermaLink="false">{escape(it.key)}</guid>',
            f"<pubDate>{format_datetime(it.first_seen.astimezone(timezone.utc))}</pubDate>",
            f"<category>{escape(it.category)}</category>",
            f'<source url="{escape(it.source_url, attr)}">{escape(it.source)}</source>',
            f"<description>{escape(item_html(it))}</description>",
            "</item>",
        ]
    out += ["</channel>", "</rss>"]
    path.write_text(CONTROL_CHARS.sub("", "\n".join(out)), encoding="utf-8")


def write_json(items: list[Item], meta: dict, path: Path):
    """Same items as feed.xml, as structured fields for automations (n8n, scripts)."""
    path.write_text(json.dumps({
        "title": meta["title"],
        "built": NOW.isoformat(),
        "items": [{
            "key": it.key,
            "title": it.title,
            "link": it.link,
            "kind": it.kind,
            "category": it.category,
            "source": it.source,
            "source_url": it.source_url,
            "starts": it.starts.astimezone(ATHENS).isoformat() if it.starts else None,
            "location": it.location,
            "summary": it.summary,
            "first_seen": it.first_seen.astimezone(timezone.utc).isoformat(),
        } for it in items],
    }, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=ROOT / "sources.yaml", type=Path)
    ap.add_argument("--out", default=ROOT / "docs", type=Path)
    ap.add_argument("--check", action="store_true", help="test sources, write nothing")
    ap.add_argument("--only", help="run only the source with this name")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    settings = cfg.get("settings", {})
    pats = {k: compile_patterns(v) for k, v in cfg["keywords"].items()}
    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    if args.only:
        sources = [s for s in sources if s["name"].lower() == args.only.lower()]

    results = run_all(sources, pats, settings)

    width = max((len(r.name) for r in results), default=10)
    for r in results:
        status = f"ERROR  {r.error}" if r.error else f"ok     fetched {r.fetched:>4}  kept {r.kept:>4}"
        log.info(f"{r.name:<{width}}  {status}")
    failed = sum(1 for r in results if r.error)
    log.info(f"\n{len(results) - failed}/{len(results)} sources working")
    if args.check:
        return 0

    items = dedupe([i for r in results for i in r.items])
    args.out.mkdir(parents=True, exist_ok=True)
    apply_first_seen(items, args.out / "seen.json")
    items.sort(key=lambda i: i.first_seen, reverse=True)
    items = items[: settings.get("max_items", 600)]

    meta = cfg["feed"]
    write_rss(items, meta, args.out / "feed.xml")
    write_rss([i for i in items if i.kind == "events"], meta, args.out / "feed-events.xml", "events only")
    write_rss([i for i in items if i.kind == "news"], meta, args.out / "feed-news.xml", "announcements")
    write_json(items, meta, args.out / "feed.json")
    (args.out / "status.json").write_text(json.dumps({
        "built": NOW.isoformat(),
        "items": len(items),
        "sources": [{"name": r.name, "fetched": r.fetched, "kept": r.kept, "error": r.error}
                    for r in results],
    }, ensure_ascii=False, indent=2))
    log.info(f"Wrote {len(items)} items to {args.out / 'feed.xml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
