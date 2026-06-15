#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NTU 駐衛警察隊（校園駐警隊）失物招領 scraper -> Supabase lost_items.

Mirrors scripts/scrapers/supa_crawl_lib.py (the library scraper): builds
lost_items-shaped dicts and upserts them with on_conflict on
(source_system, original_id). Run it the same way, from the repo venv:

    ./.venv/bin/python scripts/scrapers/ntu_police_scraper.py            # active items
    ./.venv/bin/python scripts/scrapers/ntu_police_scraper.py --all      # incl. older
    ./.venv/bin/python scripts/scrapers/ntu_police_scraper.py --dry-run  # no DB writes, dump JSON

WHY THIS IS DIFFERENT FROM THE LIBRARY SCRAPER
----------------------------------------------
The visitorcenter.ntu.edu.tw page is only a shell; rows load client-side from
a JSONP API on ann.cc.ntu.edu.tw. And unlike the library (one item per HTML
row), each police announcement (篇號) is a *bundle* of found items written as
free text inside 公告內容, e.g.:

    6/1：
    1.現金－女二舍旁小路
    6/8：
    1.耳罩式藍芽耳機－舊體前草地

So we can emit TWO kinds of lost_items rows per announcement:
  * PARSED(source_system="campus_police"):     one row per item line, with a
          per-item found_date/location/category. original_id = "{篇號}-{NNN}",
          NNN the item's ordinal in the announcement (append-only -> stable).
          This is the default output.
  * RAW   (source_system="campus_police_raw"): the whole announcement kept
          verbatim for reference / re-parsing later. original_id = 篇號.
          OPT-IN via --with-raw. The app hides source_system='*_raw' from the
          UI/matching, so pushing raw by default just clutters the table.

Two endpoint quirks handled here:
  * ann.cc.ntu.edu.tw's TLS cert lacks the Subject Key Identifier extension,
    which Python 3.13+/OpenSSL strict mode (VERIFY_X509_STRICT) rejects. We
    turn off ONLY that structural flag; chain + hostname verification stay on.
    (This is also why we use stdlib urllib here instead of requests.)
  * python-dotenv's find_dotenv() crashes on Python 3.14, so we load .env from
    an explicit path derived from this file's location.
"""

import argparse
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# --- Endpoint config (from the inline <script> on NtuAPINews_n_195955.html) --
LIST_URL = "https://ann.cc.ntu.edu.tw/asp/jsoplist.asp"
CONTENT_URL = "https://ann.cc.ntu.edu.tw/asp/jsopContent.asp"
SELECT_UNITA = "環境保護暨職業安全衛生中心"   # 一級單位
SELECT_UNITB = "駐衛警察隊"                    # 二級單位

SOURCE_PARSED = "campus_police"          # one row per parsed item
SOURCE_RAW = "campus_police_raw"         # one row per whole announcement (reference)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://visitorcenter.ntu.edu.tw/NtuAPINews_n_195955.html"

# lost_items column length limits (from core/models.py) — keep DB happy.
LEN_SOURCE = 50
LEN_ORIGINAL_ID = 50
LEN_FOUND_DATE = 50
LEN_CATEGORY = 100
LEN_STORAGE = 250

# Relax only the strict X.509 structural checks (see module docstring).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

_JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.DOTALL)


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def fetch(url, params=None, retries=3, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Referer": REFERER, "Accept": "*/*"}
    )
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                wait = attempt * 2
                print(f"  ! fetch failed ({exc}); retry {attempt}/{retries} in {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last_err}")


def unwrap_jsonp(text):
    m = _JSONP_RE.match(text.strip())
    return json.loads(m.group(1) if m else text)


def decode(value):
    """Recursively decode HTML numeric entities in API strings."""
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    return value


def get_list(include_old):
    params = {
        "oldpost": "1" if include_old else "0",
        "select_unita": SELECT_UNITA,
        "select_unitb": SELECT_UNITB,
    }
    data = decode(unwrap_jsonp(fetch(LIST_URL, params)))
    return data.get("文章列表", {}).get("文章", []) or []


def get_detail(num):
    data = decode(unwrap_jsonp(fetch(CONTENT_URL, {"num": num})))
    return data.get("文章", {}) or {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clip(text, max_length):
    if text is None:
        return None
    text = " ".join(str(text).split()) if max_length and max_length <= LEN_STORAGE else str(text)
    return text[:max_length] if max_length and len(text) > max_length else text


def norm_date(s):
    """Normalise '2026-06-08' / '2026/6/8' -> '2026/06/08'. None on failure."""
    if not s:
        return None
    m = re.match(r"\s*(\d{4})\D(\d{1,2})\D(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    return f"{y:04d}/{mo:02d}/{d:02d}"


# Dashes that separate "item－location" in the body.
_DASHES = "－—–"  # fullwidth dash is the site convention; em/en as fallback
_DATE_HEADER_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*[:：]?\s*$")
_ITEM_RE = re.compile(r"^\s*\d+\s*[.\．、)]\s*(.+?)\s*$")
# Lines that mark the start of the boilerplate footer (no items after these).
_BOILERPLATE_RE = re.compile(
    r"如為遺失|請攜帶|認領|本隊位置|Location of|If you are the owner|地圖|NTU Map"
)

# Second body format: labeled single/multi item blocks, e.g.
#   拾獲品項：現金 / 拾獲地點：小椰林道水溝蓋附近 / 拾獲時間：2025/8/1 12:35
_LABEL_NAME_RE = re.compile(r"拾獲品項[：:]\s*(.+)")
_LABEL_LOC_RE = re.compile(r"拾獲地點[：:]\s*(.+)")
_LABEL_TIME_RE = re.compile(r"拾獲時間[：:]\s*(\d{4}\D\d{1,2}\D\d{1,2})")

# Best-effort category from the item name (kept short for category String(100)).
_CATEGORY_RULES = [
    (re.compile(r"現金|鈔|錢(?!包)"), "現金"),
    (re.compile(r"耳機|耳罩|airpods|earphone|earbud", re.I), "電子產品"),
    (re.compile(r"手機|iphone|筆電|平板|ipad|充電|行動電源|手錶|watch|相機", re.I), "電子產品"),
    (re.compile(r"悠遊卡|一卡通|學生證|身分證|信用卡|證件|卡片|提款卡|金融卡"), "證件卡片"),
    (re.compile(r"錢包|皮夾|背包|包$|手提包|side ?bag", re.I), "包類"),
    (re.compile(r"鑰匙|鎖匙|key", re.I), "鑰匙"),
    (re.compile(r"雨傘|傘|umbrella", re.I), "雨傘"),
    (re.compile(r"水壺|水瓶|保溫瓶|瓶"), "水壺"),
    (re.compile(r"眼鏡|glasses", re.I), "眼鏡"),
    (re.compile(r"外套|衣|帽|圍巾|手套"), "衣物"),
]


def classify(name):
    for rx, label in _CATEGORY_RULES:
        if rx.search(name):
            return label
    return None


def extract_storage_place(body_text, phone):
    """Build a storage_place string from the body's office line + phone."""
    office = None
    m = re.search(r"本隊位置[：:]\s*(.+?)。", body_text)
    if m:
        office = m.group(1).strip()
    parts = [office or "駐衛警察隊辦公室"]
    if phone:
        parts.append(f"Tel: {phone}")
    return clip(" / ".join(parts), LEN_STORAGE)


def clean_body(raw):
    """<BR> -> newlines, strip other tags, second entity pass."""
    if not raw:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(ln.strip() for ln in text.splitlines()).strip()


def split_item(content):
    """'現金－女二舍旁小路' -> ('現金', '女二舍旁小路'). location None if no dash."""
    for dash in _DASHES:
        if dash in content:
            name, loc = content.split(dash, 1)
            return name.strip(), loc.strip() or None
    return content.strip(), None


def parse_items(body_text, ann_year, ann_month):
    """Yield {found_date, name, location} dicts from a cleaned announcement body."""
    items = []
    cur_date = None  # (year, month, day)
    for line in body_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _BOILERPLATE_RE.search(line):
            break
        hdr = _DATE_HEADER_RE.match(line)
        if hdr:
            mo, d = int(hdr.group(1)), int(hdr.group(2))
            # Items are always past finds: a month later than the announcement's
            # month must belong to the previous year (e.g. Dec item in a Jan post).
            year = ann_year - 1 if mo > ann_month else ann_year
            cur_date = (year, mo, d)
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        name, location = split_item(m.group(1))
        if not name:
            continue
        if cur_date:
            found_date = f"{cur_date[0]:04d}/{cur_date[1]:02d}/{cur_date[2]:02d}"
        else:
            found_date = None  # caller fills with announcement date
        items.append({"found_date": found_date, "name": name, "location": location})
    return items


def parse_labeled(body_text, ann_date):
    """Parse the 拾獲品項/拾獲地點/拾獲時間 labeled format (one block per item)."""
    items = []
    for block in re.split(r"(?=拾獲品項[：:])", body_text):
        if "拾獲品項" not in block:
            continue
        bp = _BOILERPLATE_RE.search(block)  # trim trailing footer from last block
        if bp:
            block = block[:bp.start()]
        mname = _LABEL_NAME_RE.search(block)
        if not mname:
            continue
        name = mname.group(1).splitlines()[0].strip()
        if not name:
            continue
        mloc = _LABEL_LOC_RE.search(block)
        location = mloc.group(1).splitlines()[0].strip() if mloc else None
        mtime = _LABEL_TIME_RE.search(block)
        found_date = norm_date(mtime.group(1)) if mtime else None
        items.append({
            "found_date": found_date or ann_date,
            "name": name,
            "location": location or None,
        })
    return items


# --------------------------------------------------------------------------- #
# Row building
# --------------------------------------------------------------------------- #
def build_rows(summary, detail):
    """Return (raw_row, [parsed_rows]) for one announcement."""
    pian = str(summary.get("篇號", "")).strip()
    ann_date = norm_date(summary.get("公告日期") or detail.get("公告日期"))
    body_text = clean_body(detail.get("公告內容", ""))
    phone = (detail.get("聯絡電話") or "").strip() or None
    storage = extract_storage_place(body_text, phone)

    # year/month for per-item date inference
    if ann_date:
        ann_year, ann_month = int(ann_date[:4]), int(ann_date[5:7])
    else:
        now = datetime.now()
        ann_year, ann_month = now.year, now.month

    raw_row = {
        "source_system": SOURCE_RAW,
        "original_id": clip(pian, LEN_ORIGINAL_ID),
        "found_date": clip(ann_date or datetime.now().strftime("%Y/%m/%d"), LEN_FOUND_DATE),
        "location": None,
        "description": body_text or summary.get("公告主旨"),
        "category": None,
        "storage_place": storage,
    }

    # An announcement uses one of two body formats; the parsers are mutually
    # exclusive in practice (numbered needs "1." lines, labeled needs 拾獲品項).
    items = parse_items(body_text, ann_year, ann_month) + parse_labeled(body_text, ann_date)

    parsed_rows = []
    # original_id is 篇號 + the item's ordinal in the announcement. Police posts
    # are append-only daily logs, so an item keeps its index across re-scrapes
    # (a content hash would instead collapse distinct same-name finds, e.g. two
    # separate 現金 entries with no location, into one row).
    for idx, item in enumerate(items):
        fdate = item["found_date"] or ann_date or datetime.now().strftime("%Y/%m/%d")
        name, location = item["name"], item["location"]
        parsed_rows.append({
            "source_system": SOURCE_PARSED,
            "original_id": clip(f"{pian}-{idx:03d}", LEN_ORIGINAL_ID),
            "found_date": clip(fdate, LEN_FOUND_DATE),
            "location": location,
            "description": name,
            "category": clip(classify(name), LEN_CATEGORY),
            "storage_place": storage,
        })
    return raw_row, parsed_rows


def scrape(include_old=False, delay=0.5, limit=None, with_raw=False):
    summaries = get_list(include_old)
    if limit:
        summaries = summaries[:limit]
    print(f"Found {len(summaries)} announcement(s); fetching details...")

    rows, no_items = [], []
    for i, summ in enumerate(summaries, 1):
        num = summ.get("篇號")
        try:
            detail = get_detail(num)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! [{i}] detail failed for {num}: {exc}", file=sys.stderr)
            continue
        raw_row, parsed = build_rows(summ, detail)
        # Raw (whole-announcement) rows are opt-in: they are reference-only and
        # the app filters source_system='*_raw' out of the UI/matching. Pushing
        # them by default previously cluttered the site, so default to off.
        if with_raw:
            rows.append(raw_row)
        rows.extend(parsed)
        if not parsed:
            no_items.append(num)
        print(f"  [{i}/{len(summaries)}] 篇號={num}  {summ.get('公告日期','')}  "
              f"-> {len(parsed)} item(s)")
        if delay and i < len(summaries):
            time.sleep(delay)

    if no_items:
        print(f"\n  NOTE: {len(no_items)} announcement(s) yielded 0 parsed items "
              f"(kept as raw only): {', '.join(map(str, no_items))}", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------- #
# Supabase upsert (mirrors supa_crawl_lib.py)
# --------------------------------------------------------------------------- #
def load_env():
    """Load repo .env via explicit path (find_dotenv() crashes on py3.14)."""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(dotenv_path=str(env_path))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! dotenv load skipped: {exc}", file=sys.stderr)


def push_to_supabase(rows, batch_size=500):
    from supabase import create_client, Client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Error: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing in env/.env",
              file=sys.stderr)
        sys.exit(1)

    supabase: Client = create_client(url, key)
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        supabase.table("lost_items").upsert(
            batch, on_conflict="source_system, original_id"
        ).execute()
        total += len(batch)
        print(f"  upserted {total}/{len(rows)}")
    print(f"Done. Upserted {total} rows into lost_items.")


def main():
    ap = argparse.ArgumentParser(description="Scrape NTU campus-security Lost & Found into lost_items.")
    ap.add_argument("--all", action="store_true",
                    help="include older/expired posts (oldpost=1) not just active ones")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between detail requests (default 0.5)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process first N announcements (testing)")
    ap.add_argument("--with-raw", action="store_true",
                    help="also emit whole-announcement rows (source_system=campus_police_raw); "
                         "off by default — these are reference-only and hidden from the site")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not touch the DB; write rows to --out instead")
    ap.add_argument("--out", default="ntu_police_rows.json",
                    help="dry-run output path (default ntu_police_rows.json)")
    args = ap.parse_args()

    rows = scrape(include_old=args.all, delay=args.delay, limit=args.limit, with_raw=args.with_raw)
    raw = sum(1 for r in rows if r["source_system"] == SOURCE_RAW)
    parsed = len(rows) - raw
    print(f"\nBuilt {len(rows)} rows ({parsed} parsed items"
          + (f", {raw} raw announcements" if raw else ", raw disabled") + ").")

    if args.dry_run:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"[dry-run] wrote rows to {args.out} (no DB writes).")
        return

    load_env()
    push_to_supabase(rows)


if __name__ == "__main__":
    main()
