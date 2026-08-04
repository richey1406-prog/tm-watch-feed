#!/usr/bin/env python3
"""Build per-publication-date trademark watch feeds from USPTO TDXF daily files.

Downloads the Trademark Daily XML Applications product (TRTDXFAP) from the
USPTO Open Data Portal, extracts every case-file that carries a
published-for-opposition-date, and merges each into feed/YYYY-MM-DD.json in
the shape TTLGpt's watch parser expects:

    {"publications": [{"serialNumber": "...", "markText": "...",
                       "internationalClasses": [35, 41]}, ...]}

Publication notices appear in daily files ~3 weeks before the publication
Tuesday, so feeds accumulate to completeness well before they are fetched.
Feed files are also pre-created empty through tomorrow so quiet days return
an empty publications list instead of a 404.

Modes:
    --daily                     process yesterday's file (UTC); for cron
    --dates 2026-07-06:2026-08-02   process a range of file dates; for seeding

Requires USPTO_ODP_API_KEY in the environment (an Open Data Portal key —
note this is NOT a TSDR key; the two are different key types).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

FILE_API = "https://api.uspto.gov/api/v1/datasets/products/files/TRTDXFAP/apc{yymmdd}.zip"
FEED_DIR = Path(__file__).parent / "feed"
PAST_WINDOW_DAYS = 3     # merge publication dates up to this far before the file date
FUTURE_WINDOW_DAYS = 60  # sanity cap on how far ahead a publication date may be
ENSURE_AHEAD_DAYS = 2    # pre-create empty feeds through file date + this many days


def _api_key() -> str:
    key = (os.environ.get("USPTO_ODP_API_KEY") or "").strip()
    if not key:
        sys.exit("USPTO_ODP_API_KEY is not set")
    return key


def download_file(api_key: str, file_date: date, cache_dir: Path) -> Path:
    """Fetch one daily zip. The ODP file endpoint either streams the zip
    directly (observed with urllib) or returns a prose note containing a
    signed redirect URL (observed with curl) — sniff and handle both."""
    yymmdd = file_date.strftime("%y%m%d")
    dest = cache_dir / f"apc{yymmdd}.zip"
    if dest.exists() and zipfile.is_zipfile(dest):
        return dest
    req = urllib.request.Request(
        FILE_API.format(yymmdd=yymmdd), headers={"X-API-KEY": api_key}
    )
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=120) as resp:
        head = resp.read(4)
        if head[:2] == b"PK":
            with open(tmp, "wb") as out:
                out.write(head)
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
        else:
            note = (head + resp.read()).decode("utf-8", errors="replace")
            match = re.search(r"https://\S+", note)
            if not match:
                raise RuntimeError(f"no zip and no redirect URL for {yymmdd}")
            # The URL is embedded in prose; a sentence period can trail the
            # final Key-Pair-Id parameter and corrupt the signature.
            urllib.request.urlretrieve(match.group(0).rstrip("."), tmp)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download for {yymmdd} was not a valid zip")
    tmp.replace(dest)
    return dest


def parse_pub_date(raw: str | None) -> date | None:
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def extract_publications(zip_path: Path, file_date: date) -> dict[date, dict[str, dict]]:
    """Stream the daily XML; return {publication_date: {serial: entry}}."""
    lo = file_date - timedelta(days=PAST_WINDOW_DAYS)
    hi = file_date + timedelta(days=FUTURE_WINDOW_DAYS)
    found: dict[date, dict[str, dict]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        with zf.open(xml_name) as fh:
            for _event, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag != "case-file":
                    continue
                header = elem.find("case-file-header")
                if header is not None:
                    pub = parse_pub_date(
                        header.findtext("published-for-opposition-date")
                    )
                    serial = "".join(
                        ch for ch in (elem.findtext("serial-number") or "") if ch.isdigit()
                    )
                    mark = (header.findtext("mark-identification") or "").strip()
                    if pub and lo <= pub <= hi and serial and mark:
                        classes = sorted(
                            {
                                int(c.text)
                                for c in elem.iter("international-code")
                                if c.text and c.text.strip().isdigit()
                            }
                        )
                        found.setdefault(pub, {})[serial] = {
                            "serialNumber": serial,
                            "markText": mark,
                            "internationalClasses": classes,
                        }
                elem.clear()
    return found


def feed_path(day: date) -> Path:
    return FEED_DIR / f"{day.isoformat()}.json"


def load_feed(day: date) -> dict[str, dict]:
    path = feed_path(day)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {e["serialNumber"]: e for e in payload.get("publications", [])}


def write_feed(day: date, entries: dict[str, dict]) -> None:
    payload = {
        "publications": [entries[s] for s in sorted(entries)],
    }
    FEED_DIR.mkdir(exist_ok=True)
    feed_path(day).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def process_file(api_key: str, file_date: date, cache_dir: Path) -> None:
    zip_path = download_file(api_key, file_date, cache_dir)
    found = extract_publications(zip_path, file_date)
    for pub_day, entries in sorted(found.items()):
        merged = load_feed(pub_day)
        before = len(merged)
        merged.update(entries)
        write_feed(pub_day, merged)
        print(
            f"{file_date} -> feed/{pub_day}.json: +{len(merged) - before} "
            f"(total {len(merged)})"
        )
    # Quiet days must serve an empty list, not a 404: pre-create through
    # file date + ENSURE_AHEAD_DAYS without touching populated feeds.
    day = file_date - timedelta(days=PAST_WINDOW_DAYS)
    stop = file_date + timedelta(days=ENSURE_AHEAD_DAYS)
    while day <= stop:
        if not feed_path(day).exists():
            write_feed(day, {})
            print(f"{file_date} -> feed/{day}.json: created empty")
        day += timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daily", action="store_true", help="process yesterday's file")
    mode.add_argument("--dates", help="file-date range YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument(
        "--cache-dir",
        default=os.environ.get("TDXF_CACHE_DIR", tempfile.gettempdir()),
        help="where downloaded zips are cached",
    )
    args = ap.parse_args()
    api_key = _api_key()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.daily:
        targets = [date.today() - timedelta(days=1)]
    else:
        start_s, _, end_s = args.dates.partition(":")
        start = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s or start_s)
        targets = [
            start + timedelta(days=i) for i in range((end - start).days + 1)
        ]

    failures = 0
    for file_date in targets:
        try:
            process_file(api_key, file_date, cache_dir)
        except Exception as exc:  # keep going through a seeding range
            failures += 1
            print(f"ERROR processing {file_date}: {exc}", file=sys.stderr)
    if failures:
        sys.exit(f"{failures} file(s) failed")


if __name__ == "__main__":
    main()
