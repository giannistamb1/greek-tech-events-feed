# Greek Tech Events — one RSS feed

Merges event platforms, community calendars, Greek tech media and newsletters into a single RSS feed of tech, startup, AI and developer events in Greece. It rebuilds every 3 hours on GitHub Actions for free.

**Outputs** (in `docs/`):

- `feed.xml` contains everything. Subscribe to this one.
- `feed-events.xml` contains only dated events (Meetup, Eventbrite, Luma…).
- `feed-news.xml` contains only event announcements found in news and newsletters.
- `feed.json` contains everything in `feed.xml` as structured fields (key, title, link, starts, location, source…). Automations read this one.
- `status.json` is a health report: items fetched and kept per source, plus errors.

## Where it lives

- Repo: `giannistamb1/greek-tech-events-feed`. GitHub Actions rebuilds `docs/` every 3 hours.
- Feed: `https://giannistamb1.github.io/greek-tech-events-feed/feed.xml` (GitHub Pages, `main` branch, `/docs` folder).
- JSON: `https://giannistamb1.github.io/greek-tech-events-feed/feed.json`

## Who reads it

The n8n workflow "Bonny — Opportunities Ingest + Match" reads `feed.json` after each build. It stores new items in the Airtable base "Bonny Opportunities" and matches them to people by interest and region. The workflow source is in `n8n/`.

## Make it strong: first-run checklist

Run locally:

```
pip install -r requirements.txt
python build_feed.py --check
```

This prints every source as `ok` or `ERROR`. Then:

1. **Fix sources marked `# verify` or switched off with a dated note.** Sites change URLs. If one errors, open the site, find the current listing page or its RSS link, and paste it in.
2. **Add Meetup groups.** This is the biggest win. Per-group iCal is much more reliable than Meetup search. For each relevant group (JS, Python, AI, data, UX, security, GDG, AWS, Open Coffee…), copy the `Meetup group — TEMPLATE` block and use `https://www.meetup.com/<group-name>/events/ical/`.
3. **Add Luma calendars.** On a calendar's page, click Subscribe and copy the iCal URL.
4. **Create Google Alerts with RSS delivery.** Suggested queries are in `sources.yaml`. This covers Google event search plus public LinkedIn and Facebook event pages that Google indexes.
5. **Bridge LinkedIn and Facebook.** These have no RSS or public events API, and scraping them breaks their terms. Use RSS.app, FetchRSS or similar on specific pages (e.g. Athens Tech Circle), then paste the bridge URL.
6. **Fill in the `TODO` sources** (Greek Dev News, Deasy, More.com, CNN Greece…) once you've found their RSS or listing URLs. Then set `enabled: true`.

## How filtering works

- `filter: none` keeps everything. Use it for sources that are already tech-only, like Meetup tech groups.
- `filter: tech` keeps items that match a tech keyword.
- `filter: tech_event` keeps items that match a tech keyword and an event keyword. Use it for news sites, so you get "AI Summit registrations open" but not funding news.
- `require_greece: true` keeps items that mention a Greek place. Use it for EU-wide sources.

Keywords are in `sources.yaml` and cover both English and Greek. Matching ignores accents. Too much noise? Remove broad terms like `digital` or `\bdata\b`. Missing things? Add terms.

Past events are dropped automatically. Duplicates across sources are merged (same URL, or same title on the same day). Each item keeps a stable date, so your reader won't reshuffle the feed.

## Maintenance

Check `docs/status.json` (or the Actions log) every few weeks. A source showing `ERROR` or `kept 0` for a long time needs its URL updated.
