#!/usr/bin/env python3
"""
arxiv_scraper.py

Checks arXiv daily (intended to be run on weekdays via cron) for new papers
matching a list of authors and/or keywords, both configured in a Google
Sheet. Results are emailed and pushed to a Zotero library.

Supports "blackout" date ranges (also configured in the Google Sheet) during
which no email is sent -- matching papers are queued and sent in a single
catch-up email once the blackout period ends.

Config (non-secret) lives in the Google Sheet. Secrets (SMTP + Zotero
credentials) live in config.ini next to this script -- see config.example.ini.

State (last run timestamp, seen paper ids, papers pending from a blackout)
is persisted to state.json next to this script.
"""

import configparser
import json
import logging
import smtplib
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import random

import pandas as pd
import requests
import xml.etree.ElementTree as ET

try:
    from pyzotero import zotero
except ImportError:
    zotero = None

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.ini"
ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "arxiv_scraper.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("arxiv_scraper")


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

def load_config():
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        log.error("Missing config.ini. Copy config.example.ini to config.ini and fill it in.")
        sys.exit(1)
    cfg.read(CONFIG_PATH)
    return cfg


def load_state(state_file):
    path = SCRIPT_DIR / state_file
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"last_run_date": None, "seen_ids": [], "pending_papers": []}


def save_state(state_file, state):
    path = SCRIPT_DIR / state_file
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


# --------------------------------------------------------------------------
# Google Sheet config tabs
# --------------------------------------------------------------------------

def sheet_csv_url(sheet_id, gid):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def load_authors(cfg):
    sheet_id = cfg["google_sheet"]["sheet_id"]
    gid = cfg["google_sheet"]["authors_gid"]
    df = pd.read_csv(sheet_csv_url(sheet_id, gid))
    df = df.dropna(subset=["Last", "First"])
    return list(zip(df["Last"].str.strip(), df["First"].str.strip()))


def load_keywords(cfg):
    sheet_id = cfg["google_sheet"]["sheet_id"]
    gid = cfg["google_sheet"].get("keywords_gid", "")
    if not gid:
        return []
    df = pd.read_csv(sheet_csv_url(sheet_id, gid))
    col = df.columns[0]
    return [k.strip() for k in df[col].dropna().tolist() if k.strip()]


def load_blackouts(cfg):
    """Expects columns: Start, End (YYYY-MM-DD). Returns list of (start, end) date tuples."""
    sheet_id = cfg["google_sheet"]["sheet_id"]
    gid = cfg["google_sheet"].get("blackout_gid", "")
    if not gid:
        return []
    df = pd.read_csv(sheet_csv_url(sheet_id, gid))
    df = df.dropna(subset=["Start", "End"])
    ranges = []
    for _, row in df.iterrows():
        start = pd.to_datetime(row["Start"]).date()
        end = pd.to_datetime(row["End"]).date()
        ranges.append((start, end))
    return ranges


def is_blackout_today(blackouts, today=None):
    today = today or date.today()
    for start, end in blackouts:
        if start <= today <= end:
            return True
    return False


# --------------------------------------------------------------------------
# arXiv API query
# --------------------------------------------------------------------------

def build_query(authors, keywords, categories):
    """Build an arXiv API search_query string.

    (author OR ... OR keyword OR ...) AND (cat:astro-ph.* OR cat:astro-ph)
    """
    clauses = []
    for last, first in authors:
        clauses.append(f'au:"{last}, {first}"')
    for kw in keywords:
        clauses.append(f'abs:"{kw}"')
        clauses.append(f'ti:"{kw}"')

    if not clauses:
        raise ValueError("No authors or keywords configured -- nothing to search for.")

    match_clause = "(" + " OR ".join(clauses) + ")"

    cat_clauses = [f"cat:{c.strip()}*" for c in categories]
    cat_clause = "(" + " OR ".join(cat_clauses) + ")"

    return f"{match_clause} AND {cat_clause}"


DEFAULT_TIMEOUT = 60          # up from 30
MAX_RETRIES = 4
BACKOFF_BASE = 5              # seconds


def fetch_with_retry(url, timeout=DEFAULT_TIMEOUT, max_retries=MAX_RETRIES):
    """GET with retry + exponential backoff + jitter on timeout/connection errors."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            if attempt == max_retries:
                log.error("Giving up after %d attempts: %s", attempt, e)
                raise
            wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning("Request failed (attempt %d/%d): %s -- retrying in %.1fs",
                        attempt, max_retries, e, wait)
            time.sleep(wait)


def fetch_papers(search_query, from_date, to_date, max_results=200):
    """Query the arXiv API, paginating as needed, and filter to the date window client-side.

    from_date / to_date are `date` objects. The arXiv API's date filtering
    is done via a submittedDate range clause; we additionally verify client
    side since the API's date matching is UTC-based and can be fuzzy at the edges.
    """
    from_dt = datetime.combine(from_date, datetime.min.time())
    to_dt = datetime.combine(to_date + timedelta(days=1), datetime.min.time())

    date_clause = (
        f"submittedDate:[{from_dt.strftime('%Y%m%d%H%M')} TO {to_dt.strftime('%Y%m%d%H%M')}]"
    )
    full_query = f"{search_query} AND {date_clause}"

    papers = []
    start = 0
    page_size = 100
    while True:
        params = {
            "search_query": full_query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
        log.info("Querying arXiv API (start=%d): %s", start, full_query)
        resp = fetch_with_retry(url)
        root = ET.fromstring(resp.content)
        entries = root.findall("atom:entry", ARXIV_NS)
        if not entries:
            break

        for entry in entries:
            papers.append(parse_entry(entry))

        start += page_size
        if len(entries) < page_size or start >= max_results:
            break
        time.sleep(3)  # be polite to the arXiv API (max ~1 req / 3s)

    return papers


def parse_entry(entry):
    arxiv_id_full = entry.find("atom:id", ARXIV_NS).text.strip()
    arxiv_id = arxiv_id_full.rsplit("/abs/", 1)[-1]
    title = " ".join(entry.find("atom:title", ARXIV_NS).text.split())
    summary = " ".join(entry.find("atom:summary", ARXIV_NS).text.split())
    authors = [
        a.find("atom:name", ARXIV_NS).text
        for a in entry.findall("atom:author", ARXIV_NS)
    ]
    published = entry.find("atom:published", ARXIV_NS).text
    link = arxiv_id_full
    pdf_link = None
    for l in entry.findall("atom:link", ARXIV_NS):
        if l.get("title") == "pdf":
            pdf_link = l.get("href")
    categories = [c.get("term") for c in entry.findall("atom:category", ARXIV_NS)]

    return {
        "id": arxiv_id,
        "title": title,
        "summary": summary,
        "authors": authors,
        "published": published,
        "link": link,
        "pdf_link": pdf_link,
        "categories": categories,
    }


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(cfg, papers, subject_suffix=""):
    email_cfg = cfg["email"]

    subject = f"arXiv update: {len(papers)} new paper(s){subject_suffix}"

    lines_html = [f"<h2>{subject}</h2>"]
    for p in papers:
        lines_html.append("<hr>")
        lines_html.append(f'<p><b><a href="{p["link"]}">{p["title"]}</a></b><br>')
        lines_html.append(f'{", ".join(p["authors"])}<br>')
        lines_html.append(f'<i>{", ".join(p["categories"])}</i> &mdash; {p["published"][:10]}</p>')
        lines_html.append(f'<p>{p["summary"][:600]}{"..." if len(p["summary"]) > 600 else ""}</p>')
    html_body = "\n".join(lines_html)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = email_cfg["to_addr"]
    msg.attach(MIMEText(html_body, "html"))

    host = email_cfg["smtp_host"]
    port = email_cfg.getint("smtp_port")
    use_tls = email_cfg.getboolean("use_tls", fallback=True)
    use_ssl = email_cfg.getboolean("use_ssl", fallback=False)

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        if use_tls:
            server.starttls()

    if email_cfg.get("smtp_user"):
        server.login(email_cfg["smtp_user"], email_cfg["smtp_password"])

    server.sendmail(email_cfg["from_addr"], [a.strip() for a in email_cfg["to_addr"].split(",")], msg.as_string())
    server.quit()
    log.info("Email sent: %s", subject)


# --------------------------------------------------------------------------
# Zotero
# --------------------------------------------------------------------------

def parse_author_name(full_name):
    parts = full_name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        return {"creatorType": "author", "firstName": parts[0], "lastName": parts[1]}
    return {"creatorType": "author", "firstName": "", "lastName": full_name}


def push_to_zotero(cfg, papers):
    if zotero is None:
        log.error("pyzotero not installed -- run `pip install pyzotero`. Skipping Zotero push.")
        return

    z_cfg = cfg["zotero"]
    zot = zotero.Zotero(z_cfg["library_id"], z_cfg.get("library_type", "user"), z_cfg["api_key"])

    collection_key = z_cfg.get("collection_key", "").strip()

    items = []
    for p in papers:
        template = zot.item_template("preprint")
        template["title"] = p["title"]
        template["creators"] = [parse_author_name(a) for a in p["authors"]]
        template["abstractNote"] = p["summary"]
        template["genre"] = "preprint"
        template["repository"] = "arXiv"
        template["archiveID"] = f'arXiv:{p["id"]}'
        template["url"] = p["link"]
        template["date"] = p["published"][:10]
        template["tags"] = [{"tag": c} for c in p["categories"]]
        if collection_key:
            template["collections"] = [collection_key]
        items.append(template)

    if not items:
        return

    # pyzotero's create_items handles batching but cap at 50 per Zotero API limits
    for i in range(0, len(items), 50):
        batch = items[i:i + 50]
        resp = zot.create_items(batch)
        failed = resp.get("failed", {})
        if failed:
            log.warning("Zotero: %d item(s) failed to add: %s", len(failed), failed)
        log.info("Zotero: added %d item(s)", len(resp.get("success", {})))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    cfg = load_config()
    state_file = cfg["state"].get("state_file", "state.json")
    state = load_state(state_file)

    authors = load_authors(cfg)
    keywords = load_keywords(cfg)
    blackouts = load_blackouts(cfg)
    categories = [c.strip() for c in cfg["arxiv"].get("categories", "astro-ph").split(",")]

    last_run = (
        datetime.strptime(state["last_run_date"], "%Y-%m-%d").date()
        if state.get("last_run_date")
        else date.today() - timedelta(days=1)
    )
    today = date.today()

    seen_ids = set(state.get("seen_ids", []))
    pending = state.get("pending_papers", [])

    search_query = build_query(authors, keywords, categories)
    new_papers = fetch_papers(search_query, from_date=last_run, to_date=today)

    # de-dupe against papers we've already processed (across any run)
    fresh = [p for p in new_papers if p["id"] not in seen_ids]
    for p in fresh:
        seen_ids.add(p["id"])

    log.info("Found %d new paper(s) since %s (%d after de-dupe)", len(new_papers), last_run, len(fresh))

    in_blackout = is_blackout_today(blackouts, today)

    if in_blackout:
        log.info("Today (%s) is a blackout day -- queuing %d paper(s), not sending.", today, len(fresh))
        pending.extend(fresh)
        state["pending_papers"] = pending
    else:
        to_send = pending + fresh
        pending = []
        state["pending_papers"] = []

        if to_send:
            # de-dupe within the combined batch by id, preserving order
            deduped = list({p["id"]: p for p in to_send}.values())
            suffix = " (includes catch-up from blackout period)" if to_send != fresh else ""

            try:
                send_email(cfg, deduped, subject_suffix=suffix)
            except Exception:
                log.exception("Failed to send email -- will retry next run (re-queuing papers).")
                state["pending_papers"] = deduped
                deduped = []  # don't push half-sent batch to Zotero either

            if deduped and cfg.has_section("zotero") and cfg["zotero"].get("api_key"):
                try:
                    push_to_zotero(cfg, deduped)
                except Exception:
                    log.exception("Failed to push to Zotero.")
        else:
            log.info("No new papers to send.")

    state["last_run_date"] = today.strftime("%Y-%m-%d")
    save_state(state_file, state)


if __name__ == "__main__":
    main()
