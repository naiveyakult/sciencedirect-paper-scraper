---
name: sciencedirect-paper-scraper
description: Use Chrome DevTools Protocol to dynamically crawl ScienceDirect search results for Open Access/Open Archive papers across arbitrary journals and keywords, download PDF full text, save JSON metadata, monitor CAPTCHA/content-error states, and retry failed PDF captures.
metadata:
  short-description: Scrape ScienceDirect OA PDFs and metadata
---

# ScienceDirect Paper Scraper

Use this skill when the user wants to crawl `https://www.sciencedirect.com` search results for a journal, one or more keywords, Open Access/Open Archive PDFs, metadata JSON, or failed PDF retry.

## Workflow

1. Confirm or infer the journal name, keywords, and any user-provided ScienceDirect search URLs.
2. Start visible Chrome with a remote debugging port and a dedicated profile.
3. Run `scripts/sciencedirect_cdp_crawler.py` with `SD_*` environment variables.
4. Monitor the log, PDF/JSON counts, and `failed_pdfs.jsonl`.
5. If ScienceDirect asks for CAPTCHA, tell the user to complete it in the visible Chrome window; the crawler waits and resumes.
6. Run `scripts/retry_failed_pdfs.py` after the main crawl to retry only unsaved failed PII records.

## Output Contract

The default save layout is:

```text
<journal>/<keyword>/00_<paper title>.pdf
<journal>/<keyword>/00_<paper title>.json
<journal>/<keyword>/failed_pdfs.jsonl
```

Each keyword directory has independent numbering. JSON metadata includes at least `pii`, `doi`, `title`, `abstract`, `journal`, `keyword`, `article_url`, `pdf_url`, and `scraped_at` when available.

## Main Crawler

Run from the workspace where output should be saved. Example PowerShell setup:

```powershell
$env:SD_JOURNAL='Finance Research Letters'
$env:SD_KEYWORDS='investment|portfolio'
$env:SD_CDP_PORT='9350'
$env:SD_SEARCH_CAPTCHA_TIMEOUT='7200'
python C:\Users\viruser.v-desktop\.codex\skills\sciencedirect-paper-scraper\scripts\sciencedirect_cdp_crawler.py
```

For user-provided ScienceDirect search URLs:

```powershell
$env:SD_SEARCH_URLS='https://www.sciencedirect.com/search?qs=portfolio&pub=Finance%20Research%20Letters'
$env:SD_CDP_PORT='9350'
python C:\Users\viruser.v-desktop\.codex\skills\sciencedirect-paper-scraper\scripts\sciencedirect_cdp_crawler.py
```

Use `|` or newlines to pass multiple URLs. The script preserves query parameters from each URL and only updates `show`, `offset`, and `accessTypes`.

## Failed Retry

After the main crawl, retry unsaved failed PII records:

```powershell
$env:SD_JOURNAL='Finance Research Letters'
$env:SD_RETRY_KEYWORDS='portfolio|investment'
$env:SD_CDP_PORT='9351'
python C:\Users\viruser.v-desktop\.codex\skills\sciencedirect-paper-scraper\scripts\retry_failed_pdfs.py
```

The retry script first skips already saved PII, then copies PDFs already captured under other keyword directories, then attempts dynamic PDF recapture.

## Key Environment Variables

- `SD_JOURNAL`: journal/output root name.
- `SD_KEYWORDS`: `|` separated keyword list.
- `SD_SEARCH_URLS`: optional direct ScienceDirect search URLs, separated by `|` or newlines.
- `SD_CDP_PORT`: Chrome DevTools port.
- `SD_START_OFFSET`, `SD_OFFSET_STEP`: offset sharding controls.
- `SD_OUT_ROOT`: override output root; defaults to `SD_JOURNAL`.
- `SD_MAIN_SKIP_FAILED_AFTER`: repeated-failure skip threshold; use `999` for deep retry.
- `SD_SEARCH_CAPTCHA_TIMEOUT`: search CAPTCHA wait seconds; use `7200` for long manual verification.
- `SD_BETWEEN_ARTICLES_SECONDS`, `SD_BETWEEN_PAGES_SECONDS`: comma ranges such as `4,9`.
- `SD_USE_COOKIE_FILE`, `SD_COOKIE_FILE`: optional cookie injection, default off.

## Operational Notes

- Prefer one visible Chrome/port per keyword or retry job; increase concurrency only after a stable run.
- Do not claim completion until PDF and JSON counts match.
- ScienceDirect may return temporary content-error pages or institution-login pages; cool down and reopen with a clean profile when needed.
- This skill is for public Open Access/Open Archive retrieval only; do not bypass paywalls.
