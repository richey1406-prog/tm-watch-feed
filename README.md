# tm-watch-feed

Daily-built JSON feeds of USPTO trademark applications **published for
opposition**, one file per publication date, in the pinned shape consumed by
the TTLGpt platform's trademark watch service (WO-283):

```json
{"publications": [{"serialNumber": "97123456", "markText": "EXAMPLE MARK",
                   "internationalClasses": [35, 41]}]}
```

The platform's `TRADEMARK_WATCH_FEED_URL` template points at
`feed/{date}.json` in this repo (raw URL).

## How it works

A scheduled GitHub Action (`.github/workflows/build-feed.yml`, 09:30 UTC
daily) downloads the previous day's **Trademark Daily XML Applications**
file (product `TRTDXFAP`) from the USPTO Open Data Portal and merges every
case-file carrying a `published-for-opposition-date` into that date's feed
file.

Design notes, learned against the real data:

- **Publication notices appear ~3 weeks ahead** of the publication Tuesday,
  so each Tuesday's feed accumulates to completeness days before the
  platform ingests it. The platform ingests "yesterday" around 02:15 UTC —
  before USPTO even posts yesterday's file — and this advance accumulation
  is what makes that timing safe.
- **Quiet days serve an empty list, not a 404**: feeds are pre-created empty
  through tomorrow so non-Tuesday fetches don't record false gap days.
- **Signed-URL budget**: the ODP file endpoint allows ~20 signed-URL
  requests per file per year; the builder requests each file once and caches
  zips during seeding.
- The download endpoint returns the signed URL embedded in prose (a
  trailing sentence period will corrupt the `Key-Pair-Id` if not stripped —
  yes, really).

## Secrets

`USPTO_ODP_API_KEY` (Actions secret) — an **Open Data Portal** key from
data.uspto.gov. Note ODP keys and TSDR keys are different; a TSDR key will
not work here and vice versa.

## Operations

- Seeding/backfill: `python build_feed.py --dates 2026-07-06:2026-08-02`
- Manual run: Actions tab → build-feed → Run workflow.
- If the Action ever stops (GitHub disables schedules only after 60 days of
  repo inactivity — its own commits keep it alive), the platform records
  missed days as feed gaps in the weekly watch digest rather than failing
  silently.
