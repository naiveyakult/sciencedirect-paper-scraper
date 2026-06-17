import base64
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE = "https://www.sciencedirect.com"
JOURNAL = os.environ.get("SD_JOURNAL", "Finance Research Letters").strip()
SEARCH_URLS = [
    part.strip()
    for part in re.split(r"[\r\n|]+", os.environ.get("SD_SEARCH_URLS", ""))
    if part.strip()
]
KEYWORDS = [
    part.strip()
    for part in os.environ.get("SD_KEYWORDS", "investment|portfolio|machine learning").split("|")
    if part.strip()
]
if SEARCH_URLS:
    parsed_keywords = []
    for raw_url in SEARCH_URLS:
        parsed = urllib.parse.urlparse(raw_url)
        query = urllib.parse.parse_qs(parsed.query)
        parsed_keywords.append((query.get("qs") or ["search"])[0].strip() or "search")
        if not os.environ.get("SD_JOURNAL") and (query.get("pub") or [""])[0].strip():
            JOURNAL = (query.get("pub") or [""])[0].strip()
    KEYWORDS = parsed_keywords
PORT = int(os.environ.get("SD_CDP_PORT", "9337"))
OUT_ROOT = Path(os.environ.get("SD_OUT_ROOT", JOURNAL))
COOKIE_FILE = Path(os.environ.get("SD_COOKIE_FILE", "sciencedirect_cookies.json"))
USE_COOKIE_FILE = os.environ.get("SD_USE_COOKIE_FILE", "0").lower() in ("1", "true", "yes", "on")
RELAX_JOURNAL_FILTER = os.environ.get("SD_RELAX_JOURNAL_FILTER", "0").lower() in ("1", "true", "yes", "on")
# Medium-stable mode: quick enough to make progress, but with enough breathing
# room for ScienceDirect PDF redirects and bot checks.
BETWEEN_ARTICLES_SECONDS = tuple(int(x) for x in os.environ.get("SD_BETWEEN_ARTICLES_SECONDS", "4,9").split(",", 1))
BETWEEN_PAGES_SECONDS = tuple(int(x) for x in os.environ.get("SD_BETWEEN_PAGES_SECONDS", "12,24").split(",", 1))
BETWEEN_KEYWORDS_SECONDS = tuple(int(x) for x in os.environ.get("SD_BETWEEN_KEYWORDS_SECONDS", "30,60").split(",", 1))
MAIN_SKIP_FAILED_AFTER = int(os.environ.get("SD_MAIN_SKIP_FAILED_AFTER", "2"))
CONTENT_ERROR_COOLDOWN_SECONDS = (120, 240)
START_OFFSET = int(os.environ.get("SD_START_OFFSET", "0"))
OFFSET_STEP = int(os.environ.get("SD_OFFSET_STEP", "25"))
SEARCH_CAPTCHA_TIMEOUT = int(os.environ.get("SD_SEARCH_CAPTCHA_TIMEOUT", "7200"))
SEARCH_URL_BY_KEYWORD = dict(zip(KEYWORDS, SEARCH_URLS))


def slow_pause(label, seconds_range):
    seconds = random.uniform(*seconds_range)
    print(f"[pause] {label}: {seconds:.0f}s")
    time.sleep(seconds)


def safe_name(text, limit=140):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text).rstrip(". ")
    return (text[:limit].rstrip() or "untitled")


def article_id_from_url(url):
    m = re.search(r"/science/article/pii/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def wait_for_cdp(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return http_json("/json/version")
        except Exception as exc:
            print(f"[cdp] unavailable; waiting: {str(exc)[:120]}")
            time.sleep(20)
    raise RuntimeError("Chrome DevTools did not recover before timeout.")


class Tab:
    def __init__(self, info):
        self.info = info
        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self.msg_id = 0
        self.send("Page.enable", wait=False)
        self.send("Runtime.enable", wait=False)
        self.send("Network.enable", wait=False)
        time.sleep(0.2)

    def send(self, method, params=None, wait=False, timeout=30):
        self.msg_id += 1
        msg_id = self.msg_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        if wait:
            return self.wait_id(msg_id, timeout=timeout)
        return msg_id

    def wait_id(self, msg_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, min(2.0, deadline - time.time())))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg
        return {}

    def navigate(self, url, wait=25):
        self.send("Page.navigate", {"url": url}, wait=False)
        time.sleep(wait)

    def eval(self, expression, timeout=30):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            wait=True,
            timeout=timeout,
        )
        return res.get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            http_json(f"/json/close/{self.info['id']}")
        except Exception:
            pass


def open_tab(url="about:blank"):
    encoded = urllib.parse.quote(url, safe=":/?&=%")
    last_error = None
    for _ in range(3):
        try:
            info = http_json(f"/json/new?{encoded}", method="PUT")
        except Exception as exc:
            last_error = exc
            wait_for_cdp(timeout=1800)
            time.sleep(2)
            continue
        try:
            return Tab(info)
        except Exception as exc:
            last_error = exc
            try:
                http_json(f"/json/close/{info['id']}")
            except Exception:
                pass
            time.sleep(1)
    raise last_error


def cleanup_extra_tabs():
    try:
        tabs = http_json("/json/list")
    except Exception:
        return
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = tab.get("url") or ""
        title = tab.get("title") or ""
        if "/science/article/pii/" in url or "pdf.sciencedirectassets.com" in url or title.endswith("- ScienceDirect"):
            try:
                http_json(f"/json/close/{tab['id']}")
            except Exception:
                pass


def load_cookies_into_tab(tab):
    if not USE_COOKIE_FILE:
        print("[cookies] skipped; using public ScienceDirect session for open-access papers")
        return
    if not COOKIE_FILE.exists():
        print(f"[cookies] not found: {COOKIE_FILE}")
        return
    raw = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]
    cookies = []
    for c in raw if isinstance(raw, list) else []:
        name = c.get("name") or c.get("Name")
        value = c.get("value") or c.get("Value")
        if not name or value is None:
            continue
        item = {
            "name": str(name),
            "value": str(value),
            "domain": c.get("domain") or c.get("Domain") or ".sciencedirect.com",
            "path": c.get("path") or c.get("Path") or "/",
            "secure": bool(c.get("secure", c.get("Secure", True))),
            "httpOnly": bool(c.get("httpOnly", c.get("HttpOnly", False))),
        }
        expires = c.get("expirationDate", c.get("expires"))
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        cookies.append(item)
    if cookies:
        tab.send("Network.setCookies", {"cookies": cookies}, wait=True, timeout=15)
        print(f"[cookies] injected {len(cookies)} cookie(s)")


def search_url(keyword, offset=0):
    raw_url = SEARCH_URL_BY_KEYWORD.get(keyword)
    if raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        params = urllib.parse.parse_qs(parsed.query)
        params["show"] = ["25"]
        params["offset"] = [str(offset)]
        params["accessTypes"] = ["openaccess"]
        params["lastSelectedFacet"] = ["accessTypes"]
        query = urllib.parse.urlencode({k: v[-1] for k, v in params.items()})
        return urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc or "www.sciencedirect.com", parsed.path or "/search", "", query, ""))
    params = {
        "qs": keyword,
        "pub": JOURNAL,
        "show": "25",
        "offset": str(offset),
        "accessTypes": "openaccess",
        "lastSelectedFacet": "accessTypes",
    }
    return BASE + "/search?" + urllib.parse.urlencode(params)


def search_api_params(keyword, offset, token):
    raw_url = SEARCH_URL_BY_KEYWORD.get(keyword)
    if raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    else:
        params = {"qs": keyword, "pub": JOURNAL}
    params.update({
        "show": "25",
        "offset": str(offset),
        "accessTypes": "openaccess",
        "lastSelectedFacet": "accessTypes",
        "t": token,
        "hostname": "www.sciencedirect.com",
    })
    return params


def page_has_captcha(tab):
    try:
        text = tab.eval("document.body ? document.body.innerText : ''", timeout=5) or ""
    except Exception:
        return False
    return "Are you a robot" in text or "captcha challenge" in text


def page_has_content_error(tab):
    try:
        text = tab.eval("document.body ? document.body.innerText : ''", timeout=5) or ""
    except Exception:
        return False
    return (
        "There was a problem providing the content you requested" in text
        or ("Reference number:" in text and "Please contact our support team" in text)
    )


def wait_out_content_error(tab, label):
    if not page_has_content_error(tab):
        return False
    print(f"[content-error] {label}: ScienceDirect temporary content error page.")
    slow_pause("cool down after content error", CONTENT_ERROR_COOLDOWN_SECONDS)
    return True


def wait_out_captcha(tab, label, timeout=7200):
    if not page_has_captcha(tab):
        return
    print(f"[captcha] {label}: ScienceDirect is asking for human verification.")
    print("[captcha] Please complete it in the visible Chrome window; crawler will continue automatically.")
    deadline = time.time() + timeout
    clear_checks = 0
    while time.time() < deadline:
        time.sleep(8)
        if page_has_captcha(tab):
            clear_checks = 0
            continue
        clear_checks += 1
        if clear_checks >= 2:
            print("[captcha] cleared; resuming stable mode")
            time.sleep(8)
            return
    raise RuntimeError("CAPTCHA was not cleared before timeout.")


def page_has_org_login(tab):
    try:
        text = tab.eval("document.body ? document.body.innerText : ''", timeout=5) or ""
    except Exception:
        return False
    return "查找您的组织" in text or "Find your institution" in text


def wait_for_result_items(tab, keyword, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page_has_captcha(tab):
            raise RuntimeError("Chrome is on ScienceDirect CAPTCHA page.")
        if page_has_content_error(tab):
            raise RuntimeError("Chrome is on ScienceDirect content error page.")
        if page_has_org_login(tab):
            raise RuntimeError("Chrome is on Elsevier institution login page.")
        try:
            state = tab.eval(
                """(() => ({
                  title: document.title,
                  href: location.href,
                  resultItems: document.querySelectorAll('li.ResultItem').length,
                  hasResultsText: /\\bresults\\b/i.test(document.body ? document.body.innerText : '')
                }))()"""
            ) or {}
        except websocket.WebSocketTimeoutException:
            print(f"[{keyword}] waiting for result items... websocket timeout")
            time.sleep(random.uniform(10, 20))
            continue
        if state.get("resultItems", 0) > 0:
            return state
        print(f"[{keyword}] waiting for result items... title={state.get('title')} href={state.get('href')}")
        time.sleep(random.uniform(10, 20))
    raise RuntimeError(f"Result items did not load for keyword: {keyword}")


def wait_for_search_token(tab, keyword, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page_has_captcha(tab):
            wait_out_captcha(tab, f"{keyword} search")
        if page_has_content_error(tab):
            wait_out_content_error(tab, f"{keyword} search token")
            raise RuntimeError("Chrome is on ScienceDirect content error page.")
        if page_has_org_login(tab):
            raise RuntimeError("Chrome is on Elsevier institution login page.")
        token = tab.eval(
            """(() => {
              const html = document.documentElement.outerHTML;
              const m = html.match(/"searchToken":"([^"]+)"/);
              return m ? m[1] : "";
            })()"""
        )
        if token:
            return token
        state = tab.eval("({title: document.title, href: location.href, text: document.body ? document.body.innerText.slice(0, 120) : ''})") or {}
        print(f"[{keyword}] waiting for search token... title={state.get('title')} href={state.get('href')}")
        time.sleep(random.uniform(5, 10))
    raise RuntimeError(f"Search token did not load for keyword: {keyword}")


def page_search_token(tab):
    try:
        return tab.eval(
            """(() => {
              const html = document.documentElement.outerHTML;
              const m = html.match(/"searchToken":"([^"]+)"/);
              return m ? m[1] : "";
            })()""",
            timeout=10,
        ) or ""
    except Exception:
        return ""


def fetch_search_api_page(tab, keyword, offset):
    token = page_search_token(tab) or wait_for_search_token(tab, keyword)
    params = search_api_params(keyword, offset, token)
    api_query = urllib.parse.urlencode(params)
    last_result = {}
    for attempt in range(1, 4):
        result = tab.eval(
            """(async (apiQuery) => {
              try {
                const r = await fetch('/search/api?' + apiQuery, {
                  credentials: 'include',
                  headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'X-Requested-With': 'XMLHttpRequest'
                  }
                });
                const text = await r.text();
                return { status: r.status, contentType: r.headers.get('content-type') || '', text };
              } catch (err) {
                return { status: 0, contentType: '', text: String(err && err.message || err) };
              }
            })(""" + json.dumps(api_query) + """)""",
            timeout=90,
        ) or {}
        last_result = result
        if result.get("status") == 200:
            return json.loads(result.get("text") or "{}")
        snippet = (result.get("text") or "")[:180].replace("\n", " ")
        print(f"[{keyword}] Search API retry {attempt}/3 failed HTTP {result.get('status')}: {snippet}")
        if result.get("status") in (403, 429):
            slow_pause("cool down after Search API block", (180, 360))
        time.sleep(random.uniform(12, 24))
    snippet = (last_result.get("text") or "")[:300].replace("\n", " ")
    if last_result.get("status") in (403, 429):
        raise RuntimeError(f"Search API blocked HTTP {last_result.get('status')}: {snippet}")
    raise RuntimeError(f"Search API failed HTTP {last_result.get('status')}: {snippet}")


def parse_api_article(item):
    authors_raw = item.get("authors", [])
    authors = []
    if isinstance(authors_raw, list):
        for author in authors_raw:
            if isinstance(author, dict):
                name = author.get("name") or f"{author.get('givenName', '')} {author.get('surname', '')}".strip()
                if name:
                    authors.append(name)
            elif isinstance(author, str):
                authors.append(author)
    elif isinstance(authors_raw, dict):
        for author in authors_raw.get("authorList", []):
            name = f"{author.get('givenName', '')} {author.get('surname', '')}".strip()
            if name:
                authors.append(name)

    link = item.get("link") or ""
    if link and not link.startswith("http"):
        link = BASE + link
    pii = item.get("pii") or article_id_from_url(link)
    if not link and pii:
        link = f"{BASE}/science/article/pii/{pii}"

    pdf_info = item.get("pdf") or {}
    pdf_url = pdf_info.get("downloadLink") or ""
    if pdf_url and not pdf_url.startswith("http"):
        pdf_url = BASE + pdf_url
    if not pdf_url and pii:
        pdf_url = f"{BASE}/science/article/pii/{pii}/pdfft"

    source_title = re.sub(r"<[^>]+>", "", item.get("sourceTitle") or item.get("publicationName") or JOURNAL)
    title = re.sub(r"<[^>]+>", "", item.get("title") or "")
    abstract = re.sub(r"<[^>]+>", "", item.get("abstract") or "")
    date = (item.get("sortDate") or "")[:10]
    return {
        "doi": item.get("doi") or item.get("prism:doi") or "",
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "journal": source_title,
        "volume": item.get("volume") or "",
        "issue": item.get("issue") or "",
        "year": date[:4] if date else "",
        "date": date,
        "article_type": item.get("articleType") or "",
        "article_url": link,
        "pdf_url": pdf_url,
        "pii": pii,
        "open_access": True,
        "raw_search_item": item,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def extract_search_results(tab):
    return tab.eval(
        """(journal => {
          const out = [];
          const nodes = Array.from(document.querySelectorAll('li.ResultItem'));
          for (const node of nodes) {
            const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text || !/Open access|Open archive/i.test(text) || !/View PDF/i.test(text)) continue;
            if (journal && !text.toLowerCase().includes(journal.toLowerCase())) continue;
            const article = Array.from(node.querySelectorAll('a[href*="/science/article/pii/"]'))
              .find(a => !/pdfft|pdf/i.test(a.href));
            if (!article) continue;
            const pdf = Array.from(node.querySelectorAll('a[href*="pdfft"], a[href*="pdf"]'))
              .find(a => /pdf|pdfft/i.test((a.innerText || '') + ' ' + a.href));
            out.push({
              title: (article.innerText || '').replace(/\\s+/g, ' ').trim(),
              article_url: article.href,
              pdf_url: pdf ? pdf.href : '',
              text
            });
          }
          const seen = new Set();
          return out.filter(r => {
            if (seen.has(r.article_url)) return false;
            seen.add(r.article_url);
            return true;
          });
        })(""" + json.dumps(JOURNAL) + """)"""
    ) or []


def extract_article_metadata(tab, seed):
    return tab.eval(
        """((seed, journal) => {
          const meta = (...names) => {
            for (const name of names) {
              const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
              if (el && el.content) return el.content.trim();
            }
            return '';
          };
          const authors = Array.from(document.querySelectorAll('meta[name="citation_author"]')).map(e => e.content).filter(Boolean);
          const pdf = document.querySelector('a[href*="pdfft"], a[href*="pdf"]');
          const absNode = document.querySelector('#abstracts, .abstract');
          return {
            doi: meta('citation_doi', 'dc.Identifier', 'prism.doi'),
            title: meta('citation_title', 'dc.Title', 'og:title') || seed.title || document.title,
            abstract: meta('citation_abstract', 'description', 'dc.Description') || (absNode ? absNode.innerText.replace(/^\\s*Abstract\\s*/i, '').trim() : ''),
            authors,
            journal,
            article_url: seed.article_url,
            pdf_url: seed.pdf_url || (pdf ? pdf.href : ''),
            pii: (seed.article_url.match(/\\/science\\/article\\/pii\\/([^/?#]+)/) || [,''])[1],
            open_access: true,
            source_search_text: seed.text || '',
            scraped_at: new Date().toISOString()
          };
        })(""" + json.dumps(seed, ensure_ascii=False) + ", " + json.dumps(JOURNAL) + """)"""
    ) or {}


def capture_pdf(pdf_url):
    tab = open_tab("about:blank")
    pdf_bytes = None
    body_requests = {}
    try:
        tab.send("Fetch.enable", {"patterns": [
            {"urlPattern": "*pdf.sciencedirectassets.com/*", "requestStage": "Response"},
            {"urlPattern": "*pdfft*", "requestStage": "Response"},
        ]}, wait=True)
        tab.send("Page.navigate", {"url": pdf_url}, wait=False)
        deadline = time.time() + 45
        while time.time() < deadline:
            tab.ws.settimeout(5)
            try:
                msg = json.loads(tab.ws.recv())
            except Exception:
                continue
            method = msg.get("method")
            params = msg.get("params", {})
            if method == "Fetch.requestPaused":
                rid = params.get("requestId")
                resp = params.get("responseStatusCode")
                headers = json.dumps(params.get("responseHeaders") or [])
                url = params.get("request", {}).get("url", "")
                if resp and ("pdf" in headers.lower() or "pdf" in url.lower()):
                    body_id = tab.send("Fetch.getResponseBody", {"requestId": rid})
                    body_requests[body_id] = rid
                else:
                    tab.send("Fetch.continueRequest", {"requestId": rid})
            elif "id" in msg and msg["id"] in body_requests:
                fetch_request_id = body_requests[msg["id"]]
                result = msg.get("result", {})
                body = result.get("body")
                if body:
                    pdf_bytes = base64.b64decode(body) if result.get("base64Encoded") else body.encode()
                    if pdf_bytes[:4] == b"%PDF":
                        return pdf_bytes
                try:
                    tab.send("Fetch.continueRequest", {"requestId": fetch_request_id})
                except Exception:
                    pass
        return None
    finally:
        tab.close()


def get_pdf_url_from_article(article_url, pii):
    tab = open_tab("about:blank")
    try:
        tab.navigate(article_url, wait=16)
        wait_out_captcha(tab, f"article {pii}", timeout=7200)
        if wait_out_content_error(tab, f"article {pii}"):
            return ""
        if page_has_org_login(tab):
            return ""
        try:
            return tab.eval(
                """(pii => {
                  const links = Array.from(document.querySelectorAll('a[href*="pdfft"], a[href*="pdf"]'));
                  const exact = links.find(a => a.href.includes('/' + pii + '/pdfft'));
                  const view = links.find(a => /view\\s*pdf/i.test((a.innerText || '').replace(/\\s+/g, ' ')));
                  return (exact || view || links[0] || {}).href || '';
                })(""" + json.dumps(pii) + """)""",
                timeout=20,
            ) or ""
        except Exception as exc:
            print(f"    fallback pdf link failed: {str(exc)[:120]}")
            return ""
    finally:
        tab.close()


def record_failed_pdf(keyword, metadata, reason):
    fail_path = OUT_ROOT / keyword / "failed_pdfs.jsonl"
    fail_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "pii": metadata.get("pii"),
        "doi": metadata.get("doi"),
        "title": metadata.get("title"),
        "article_url": metadata.get("article_url"),
        "pdf_url": metadata.get("pdf_url"),
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with fail_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def existing_state(out_dir):
    seen = set()
    max_index = -1
    for pdf in out_dir.glob("*.pdf"):
        m = re.match(r"(\d+)_", pdf.name)
        if m:
            max_index = max(max_index, int(m.group(1)))
    for meta_file in out_dir.glob("*.json"):
        try:
            pii = json.loads(meta_file.read_text(encoding="utf-8")).get("pii")
            if pii:
                seen.add(pii)
        except Exception:
            pass
    return seen, max_index + 1


def pii_exists(out_dir, pii):
    if not pii:
        return False
    for meta_file in out_dir.glob("*.json"):
        try:
            if json.loads(meta_file.read_text(encoding="utf-8")).get("pii") == pii:
                return True
        except Exception:
            pass
    return False


def next_file_index(out_dir):
    max_index = -1
    for pdf in out_dir.glob("*.pdf"):
        m = re.match(r"(\d+)_", pdf.name)
        if m:
            max_index = max(max_index, int(m.group(1)))
    return max_index + 1


def acquire_save_lock(out_dir, timeout=180):
    lock_path = out_dir / ".save.lock"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(random.uniform(0.2, 0.8))
    raise RuntimeError(f"Could not acquire save lock for {out_dir}")


def release_save_lock(lock_path):
    try:
        lock_path.unlink()
    except Exception:
        pass


def save_pdf_and_metadata(out_dir, title_text, metadata, pii, pdf_bytes=None, source_pdf=None):
    lock_path = acquire_save_lock(out_dir)
    try:
        if pii_exists(out_dir, pii):
            return None
        index = next_file_index(out_dir)
        title = safe_name(title_text)
        stem = f"{index:02d}_{title}"
        pdf_path = out_dir / f"{stem}.pdf"
        meta_path = out_dir / f"{stem}.json"
        if source_pdf:
            shutil.copy2(source_pdf, pdf_path)
        else:
            pdf_path.write_bytes(pdf_bytes)
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return stem
    finally:
        release_save_lock(lock_path)


def failed_counts(keyword):
    counts = {}
    fail_path = OUT_ROOT / keyword / "failed_pdfs.jsonl"
    if not fail_path.exists():
        return counts
    for line in fail_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except Exception:
            continue
        pii = payload.get("pii")
        if pii:
            counts[pii] = counts.get(pii, 0) + 1
    return counts


def existing_download_for_pii(current_keyword, pii):
    for keyword_dir in OUT_ROOT.iterdir() if OUT_ROOT.exists() else []:
        if not keyword_dir.is_dir() or keyword_dir.name == current_keyword:
            continue
        for meta_file in keyword_dir.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("pii") != pii:
                continue
            pdf_file = meta_file.with_suffix(".pdf")
            if pdf_file.exists():
                return keyword_dir.name, pdf_file, meta_file, meta
    return None, None, None, None


def crawl_keyword(keyword):
    out_dir = OUT_ROOT / keyword
    out_dir.mkdir(parents=True, exist_ok=True)
    seen, saved = existing_state(out_dir)
    repeated_failures = failed_counts(keyword)
    print(f"[{keyword}] resume state: {len(seen)} known PII(s), next index={saved:02d}")
    if repeated_failures:
        blocked = sum(1 for count in repeated_failures.values() if count >= MAIN_SKIP_FAILED_AFTER)
        print(f"[{keyword}] main pass will defer {blocked} repeated PDF failure(s) to slow retry")
    cleanup_extra_tabs()
    page = open_tab("about:blank")
    try:
        load_cookies_into_tab(page)
        offset = START_OFFSET
        total = None
        print(f"[{keyword}] shard start_offset={START_OFFSET}, offset_step={OFFSET_STEP}")
        while True:
            url = search_url(keyword, offset)
            print(f"[{keyword}] {url}")
            dom_results = []
            for attempt in range(1, 4):
                try:
                    page.navigate(url, wait=14 + (attempt - 1) * 8)
                    wait_for_result_items(page, keyword, timeout=90)
                    dom_results = extract_search_results(page)
                    break
                except (RuntimeError, OSError, websocket.WebSocketException) as exc:
                    message = str(exc)
                    if "institution login" in message:
                        print(f"[{keyword}] institution login page appeared; reopening ScienceDirect tab")
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = open_tab("about:blank")
                        load_cookies_into_tab(page)
                        if attempt >= 3:
                            raise
                        slow_pause("recover from institution login", (30, 60))
                        continue
                    if "content error" in message:
                        print(f"[{keyword}] content error page appeared; reopening ScienceDirect tab")
                        wait_out_content_error(page, f"{keyword} search page")
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = open_tab("about:blank")
                        load_cookies_into_tab(page)
                        if attempt >= 3:
                            raise
                        continue
                    if "CAPTCHA" in message:
                        wait_out_captcha(page, f"{keyword} search page", timeout=SEARCH_CAPTCHA_TIMEOUT)
                        continue
                    if isinstance(exc, (OSError, websocket.WebSocketException)):
                        print(f"[{keyword}] search tab connection lost on attempt {attempt}; reopening tab")
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = open_tab("about:blank")
                        load_cookies_into_tab(page)
                        if attempt >= 3:
                            raise
                        slow_pause("reopen search tab", (20, 35))
                        continue
                    token = page_search_token(page)
                    if token:
                        print(f"[{keyword}] search DOM did not load; continuing with API-only mode on attempt {attempt}")
                        break
                    if attempt >= 3:
                        raise
                    print(f"[{keyword}] search page did not load on attempt {attempt}; retrying")
                    slow_pause("reload search page", (20, 35))
            dom_pdf_by_pii = {}
            for dom_result in dom_results:
                dom_pii = article_id_from_url(dom_result.get("article_url"))
                if dom_pii and dom_result.get("pdf_url"):
                    dom_pdf_by_pii[dom_pii] = dom_result["pdf_url"]
            try:
                data = fetch_search_api_page(page, keyword, offset)
            except RuntimeError as exc:
                if "Search API blocked" in str(exc):
                    print(f"[{keyword}] Search API is blocked at offset={offset}; reopening after cooldown")
                    slow_pause("recover from Search API block", (300, 600))
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = open_tab("about:blank")
                    load_cookies_into_tab(page)
                    continue
                raise
            if total is None:
                total = int(data.get("resultsFound") or data.get("totalResults") or 0)
                print(f"[{keyword}] API reported open-access results: {total}")
            items = data.get("searchResults") or []
            results = [parse_api_article(item) for item in items]
            for result in results:
                if result.get("pii") in dom_pdf_by_pii:
                    result["pdf_url"] = dom_pdf_by_pii[result["pii"]]
            results = [
                r for r in results
                if r.get("pii") and r.get("pdf_url") and (
                    RELAX_JOURNAL_FILTER or (r.get("journal") or "").lower() == JOURNAL.lower()
                )
            ]
            print(f"[{keyword}] candidates on page: {len(results)}")
            if not items:
                break
            for result in results:
                pii = result.get("pii") or article_id_from_url(result.get("article_url"))
                if not pii or pii in seen:
                    continue
                source_keyword, source_pdf, source_meta_file, source_meta = existing_download_for_pii(keyword, pii)
                if source_pdf and source_meta_file:
                    copied_meta = dict(source_meta)
                    copied_meta["copied_from_keyword"] = source_keyword
                    copied_meta["keyword"] = keyword
                    copied_meta["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    stem = save_pdf_and_metadata(
                        out_dir,
                        source_meta.get("title") or result.get("title"),
                        copied_meta,
                        pii,
                        source_pdf=source_pdf,
                    )
                    print(f"  article: {result['title'][:100]}")
                    if stem:
                        print(f"    copied from {source_keyword}: {out_dir / (stem + '.pdf')}")
                        saved += 1
                    else:
                        print(f"    skip: already saved by another shard")
                    seen.add(pii)
                    continue
                if repeated_failures.get(pii, 0) >= MAIN_SKIP_FAILED_AFTER:
                    print(f"  article: {result['title'][:100]}")
                    print(f"    defer: repeated pdf failures ({repeated_failures[pii]})")
                    continue
                seen.add(pii)
                print(f"  article: {result['title'][:100]}")
                meta = result
                pdf_url = result.get("pdf_url")
                if not pdf_url:
                    print("    skip: no pdf url")
                    continue
                if "md5=" not in pdf_url and meta.get("article_url"):
                    better_pdf_url = get_pdf_url_from_article(meta["article_url"], pii)
                    if better_pdf_url:
                        meta["pdf_url"] = better_pdf_url
                        pdf_url = better_pdf_url
                try:
                    pdf_bytes = capture_pdf(pdf_url)
                except Exception as exc:
                    print(f"    skip: pdf capture error {str(exc)[:120]}")
                    pdf_bytes = None
                if not pdf_bytes and meta.get("article_url"):
                    better_pdf_url = get_pdf_url_from_article(meta["article_url"], pii)
                    if better_pdf_url and better_pdf_url != pdf_url:
                        meta["pdf_url"] = better_pdf_url
                        try:
                            pdf_bytes = capture_pdf(better_pdf_url)
                        except Exception as exc:
                            print(f"    skip: fallback pdf capture error {str(exc)[:120]}")
                            pdf_bytes = None
                if not pdf_bytes:
                    print("    skip: pdf not captured")
                    record_failed_pdf(keyword, meta, "pdf_not_captured")
                    continue
                stem = save_pdf_and_metadata(
                    out_dir,
                    meta.get("title") or result.get("title"),
                    meta,
                    pii,
                    pdf_bytes=pdf_bytes,
                )
                if stem:
                    print(f"    saved: {out_dir / (stem + '.pdf')} ({len(pdf_bytes)//1024} KB)")
                    saved += 1
                else:
                    print("    skip: already saved by another shard")
                slow_pause("after saved PDF", BETWEEN_ARTICLES_SECONDS)
            offset += OFFSET_STEP
            if total and offset >= total:
                break
            cleanup_extra_tabs()
            slow_pause("between search pages", BETWEEN_PAGES_SECONDS)
    finally:
        page.close()
    return saved


def main():
    try:
        version = http_json("/json/version")
    except Exception as exc:
        raise SystemExit(f"Cannot connect to Chrome DevTools on port {PORT}: {exc}")
    print(f"[cdp] connected: {version.get('Browser')}")
    OUT_ROOT.mkdir(exist_ok=True)
    cleanup_extra_tabs()
    bootstrap = open_tab("about:blank")
    try:
        load_cookies_into_tab(bootstrap)
    finally:
        bootstrap.close()
    totals = {}
    for index, keyword in enumerate(KEYWORDS):
        totals[keyword] = crawl_keyword(keyword)
        if index < len(KEYWORDS) - 1:
            slow_pause("between keywords", BETWEEN_KEYWORDS_SECONDS)
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
