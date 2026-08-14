# arXiv Monitor

Checks arXiv for new papers by authors and/or keywords you care about, emails
you a digest, and adds the papers to a Zotero library. Designed to run once a
day on weekdays via cron.

Handles blackout/pause periods: during a blackout, matching papers are
queued (not emailed) and get sent in one catch-up email once the blackout
ends. They're still added to Zotero as normal once the queue is flushed —
Zotero is only touched on non-blackout runs, alongside the catch-up email.

## 1. Set up the Google Sheet

Use one spreadsheet with three tabs. It must be shared as **"Anyone with the
link can view"** (Share button, top right) since the script reads it as a
public CSV export, without any Google auth.

**Tab 1 — Authors** (same as your existing sheet)

| First | Last |
|-------|------|
| Tony  | Pahl |

**Tab 2 — Keywords** — one column, header can be anything (the script reads
whichever column is first), one keyword/phrase per row:

| Keyword |
|---------|
| gravitational lensing |
| dark matter halos |

**Tab 3 — Blackout** — date ranges during which you don't want emails:

| Start      | End        |
|------------|------------|
| 2026-12-20 | 2027-01-02 |

Dates are inclusive, `YYYY-MM-DD`.

You can name the tabs anything you like — what matters is the `gid` of each
tab. Click each tab and look at the URL: `...edit#gid=XXXXXXXXX`. Put those
numbers into `config.ini` (see below). The gid for the very first tab in a
sheet is usually `0`.

## 2. Install

```bash
git clone <this-repo-or-copy-these-files> arxiv_monitor
cd arxiv_monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.ini config.ini
```

Edit `config.ini`:

- `[google_sheet]`: your sheet ID + the three gids from step 1.
- `[arxiv]`: category filter, defaults to `astro-ph` (matches `astro-ph.*`
  sub-categories too, and cross-listings are included automatically since
  the arXiv API matches any listed category on a paper).
- `[email]`: your SMTP server details.
  - Port 587 → `use_tls = true`, `use_ssl = false` (STARTTLS, most common).
  - Port 465 → `use_ssl = true`, `use_tls = false` (implicit TLS).
  - Many providers require an **app password** rather than your normal
    login password when connecting via SMTP like this — check your
    provider's docs if login fails.

  **Using Gmail:** `config.example.ini` is already set up for
  `smtp.gmail.com` on port 587. You just need an App Password:
  1. Turn on 2-Step Verification if it isn't already: https://myaccount.google.com/security
  2. Go to https://myaccount.google.com/apppasswords, create one (name it
     something like "arxiv monitor"), and copy the 16-character password.
  3. Put your Gmail address in `smtp_user` and `from_addr`, and the app
     password (no spaces) in `smtp_password`. Your regular Gmail password
     won't work here — Google blocks plain-password SMTP logins.
  4. Gmail caps outgoing mail at 500/day, and this script sends at most one
     email per run, so you're nowhere near that limit.
- `[zotero]`:
  - `library_id`: your numeric Zotero user ID, shown at
    https://www.zotero.org/settings/keys
  - `api_key`: create one at the same URL, with **read/write** permission.
  - `collection_key`: optional — leave blank to add items to your library
    root. To scope to one collection, open it in the Zotero web library and
    copy the key from the URL (`.../collections/COLLECTION_KEY`).

## 3. Test it manually

```bash
source venv/bin/activate
python3 arxiv_monitor.py
```

First run: since there's no prior state, it looks back one day. Check
`arxiv_monitor.log` for what happened, and `state.json` gets created to
track what's already been sent.

To force a wider look-back for testing (e.g. to see if it finds anything at
all), temporarily edit `state.json`'s `last_run_date` to an older date, or
delete `state.json` — but note deleting it also clears the "already seen"
id list, so you may get duplicate emails/Zotero entries for papers around
the boundary.

## 4. Schedule it (cron)

Run on weekdays only, e.g. 8:00am server time:

```
0 8 * * 1-5 cd /path/to/arxiv_monitor && ./venv/bin/python3 arxiv_monitor.py >> cron.log 2>&1
```

Edit with `crontab -e`. Adjust the hour to your preference — since the
script always searches everything since its last successful run (not just
"today"), a missed or delayed run won't lose papers.

## 5. How the pieces fit together

- **Query**: authors and keywords are OR'd together, then AND'd with your
  category filter and a submitted-date range covering everything since the
  last successful run. This uses arXiv's official API
  (`export.arxiv.org/api/query`), not the HTML search page, so it's stable
  and returns structured data (title, authors, abstract, categories, links)
  directly — no scraping.
- **De-dup**: every paper id seen is stored in `state.json` so the same
  paper is never emailed or added to Zotero twice, even if it matches
  multiple authors/keywords or shows up again in a later query window.
- **Blackout**: on a blackout day, matches are appended to a `pending_papers`
  queue in `state.json` and nothing is sent. On the next non-blackout run,
  pending + newly-found papers are combined, de-duped, and sent as one
  email (subject line notes it's a catch-up) and pushed to Zotero.
- **Failure handling**: if the email send fails, the batch is re-queued as
  pending and retried on the next run rather than being lost; Zotero is
  only pushed after a successful email send for that batch.

## 6. Notes / things you may want to tweak

- Zotero items are added as type `preprint` with `repository = arXiv` and
  `archiveID = arXiv:XXXX.XXXXX`, tagged with the paper's arXiv categories.
  Change `push_to_zotero()` if you'd rather use `journalArticle` or a
  different field mapping.
- The email is HTML with title (linked), authors, categories, date, and a
  truncated (600 char) abstract. Adjust `send_email()` for a different
  format.
- `max_results` in `fetch_papers` caps at 200 papers per run as a safety
  limit — plenty for a daily digest, but raise it if you add a lot of broad
  keywords.
