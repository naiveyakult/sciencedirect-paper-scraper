# ScienceDirect Paper Scraper

Codex skill for crawling ScienceDirect Open Access / Open Archive search results with Chrome DevTools Protocol, saving article PDFs and JSON metadata, and retrying failed PDF captures.

## Contents

- `SKILL.md` - Codex skill instructions and output contract.
- `scripts/sciencedirect_cdp_crawler.py` - Main ScienceDirect crawler.
- `scripts/retry_failed_pdfs.py` - Retry workflow for unsaved failed PII records.
- `agents/openai.yaml` - Agent metadata.

## Requirements

```powershell
pip install playwright requests
```

Use a visible Chrome instance with remote debugging enabled. Complete CAPTCHA or manual verification in that browser when ScienceDirect asks for it.

## Example

```powershell
$env:SD_JOURNAL='Finance Research Letters'
$env:SD_KEYWORDS='investment|portfolio'
$env:SD_CDP_PORT='9350'
$env:SD_SEARCH_CAPTCHA_TIMEOUT='7200'
python scripts/sciencedirect_cdp_crawler.py
```

Retry failed PDFs:

```powershell
$env:SD_JOURNAL='Finance Research Letters'
$env:SD_RETRY_KEYWORDS='portfolio|investment'
$env:SD_CDP_PORT='9351'
python scripts/retry_failed_pdfs.py
```

