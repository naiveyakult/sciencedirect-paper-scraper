import json
import os
import time
from pathlib import Path

import sciencedirect_cdp_crawler as cdp

try:
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


KEYWORDS = [
    part.strip()
    for part in os.environ.get("SD_RETRY_KEYWORDS", os.environ.get("SD_KEYWORDS", "investment|portfolio|machine learning")).split("|")
    if part.strip()
]
LIMIT_PER_KEYWORD = int(os.environ.get("SD_RETRY_LIMIT_PER_KEYWORD", "9999"))


def load_failed(keyword):
    path = cdp.OUT_ROOT / keyword / "failed_pdfs.jsonl"
    if not path.exists():
        return []
    by_pii = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        pii = payload.get("pii")
        if pii:
            by_pii[pii] = payload
    return list(by_pii.values())


def retry_one(keyword, item):
    out_dir = cdp.OUT_ROOT / keyword
    pii = item.get("pii")
    if not pii or cdp.pii_exists(out_dir, pii):
        return "already"

    source_keyword, source_pdf, source_meta_file, source_meta = cdp.existing_download_for_pii(keyword, pii)
    if source_pdf and source_meta_file:
        meta = dict(source_meta)
        meta["copied_from_keyword"] = source_keyword
        meta["keyword"] = keyword
        meta["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        stem = cdp.save_pdf_and_metadata(out_dir, meta.get("title") or item.get("title"), meta, pii, source_pdf=source_pdf)
        if stem:
            print(f"[{keyword}] copied {pii} from {source_keyword}: {stem}")
            return "saved"
        return "already"

    meta = dict(item)
    meta["keyword"] = keyword
    pdf_url = meta.get("pdf_url") or ""
    pdf_bytes = None
    if pdf_url:
        try:
            pdf_bytes = cdp.capture_pdf(pdf_url)
        except Exception as exc:
            print(f"[{keyword}] direct capture failed {pii}: {str(exc)[:120]}")

    if not pdf_bytes and meta.get("article_url"):
        better = cdp.get_pdf_url_from_article(meta["article_url"], pii)
        if better:
            meta["pdf_url"] = better
            try:
                pdf_bytes = cdp.capture_pdf(better)
            except Exception as exc:
                print(f"[{keyword}] article capture failed {pii}: {str(exc)[:120]}")

    if not pdf_bytes:
        print(f"[{keyword}] still failed {pii}: {meta.get('title', '')[:90]}")
        return "failed"

    stem = cdp.save_pdf_and_metadata(out_dir, meta.get("title") or pii, meta, pii, pdf_bytes=pdf_bytes)
    if stem:
        print(f"[{keyword}] saved {pii}: {stem} ({len(pdf_bytes)//1024} KB)")
        return "saved"
    return "already"


def main():
    version = cdp.http_json("/json/version")
    print(f"[cdp] connected: {version.get('Browser')}")
    cdp.OUT_ROOT.mkdir(exist_ok=True)
    cdp.cleanup_extra_tabs()
    bootstrap = cdp.open_tab("about:blank")
    try:
        cdp.load_cookies_into_tab(bootstrap)
    finally:
        bootstrap.close()

    summary = {}
    for keyword in KEYWORDS:
        failed = load_failed(keyword)
        todo = [item for item in failed if not cdp.pii_exists(cdp.OUT_ROOT / keyword, item.get("pii"))]
        print(f"[{keyword}] failed records={len(failed)}, unsaved={len(todo)}")
        counts = {"saved": 0, "failed": 0, "already": 0}
        for item in todo[:LIMIT_PER_KEYWORD]:
            result = retry_one(keyword, item)
            counts[result] = counts.get(result, 0) + 1
            cdp.slow_pause("between failed retries", cdp.BETWEEN_ARTICLES_SECONDS)
        summary[keyword] = counts
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
