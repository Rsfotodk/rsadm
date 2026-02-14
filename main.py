#!/usr/bin/env python3
import json
import os
import re
import ssl
import tempfile
from datetime import datetime, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

DATA_PATH = Path(__file__).with_name("results.json")
PORT = int(os.getenv("PORT", "8787"))
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
DEFAULT_MAX_RESULTS = int(os.getenv("BYLINE_MAX_RESULTS", "10"))
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
ALLOW_INSECURE_SSL = os.getenv("ALLOW_INSECURE_SSL", "0").strip().lower() in ("1", "true", "yes")


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.send_header("access-control-allow-origin", "*")
    handler.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
    handler.send_header("access-control-allow-headers", "content-type")
    handler.end_headers()
    handler.wfile.write(body)


def _http_get(url: str, timeout: int = 25) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    context = ssl._create_unverified_context() if ALLOW_INSECURE_SSL else None
    with urlopen(req, timeout=timeout, context=context) as res:
        raw = res.read()
    return raw.decode("utf-8", errors="ignore")


def _load_results() -> List[Dict]:
    if not DATA_PATH.exists():
        return []
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_results(rows: List[Dict]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="results_", suffix=".json", dir=str(DATA_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, DATA_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return unescape(re.sub(r"\s+", " ", s)).strip()


def _search_serpapi(query: str, num: int) -> List[Dict]:
    params = {
        "engine": "google",
        "q": query,
        "num": max(1, min(100, num)),
        "api_key": SERPAPI_KEY,
        "hl": "da",
    }
    url = f"https://serpapi.com/search.json?{urlencode(params)}"
    payload = json.loads(_http_get(url))
    out: List[Dict] = []
    for r in payload.get("organic_results", []):
        link = (r.get("link") or "").strip()
        if not link:
            continue
        out.append(
            {
                "url": link,
                "domain": urlparse(link).netloc.lower(),
                "google_title": (r.get("title") or "").strip(),
                "google_snippet": (r.get("snippet") or "").strip(),
                "search_date": datetime.now().date().isoformat(),
                "published_at_raw": (r.get("date") or "").strip(),
            }
        )
    return out


def _search_duckduckgo(query: str, num: int) -> List[Dict]:
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
    html = _http_get(url)
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    out: List[Dict] = []
    seen = set()
    for href, title_html in pattern.findall(html):
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc:
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            link = target or href
        else:
            link = href
        link = link.strip()
        if not link or not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        out.append(
            {
                "url": link,
                "domain": urlparse(link).netloc.lower(),
                "google_title": _strip_html(title_html),
                "google_snippet": "",
                "search_date": datetime.now().date().isoformat(),
                "published_at_raw": "",
            }
        )
        if len(out) >= max(1, min(50, num)):
            break
    return out


def _fetch_page_title_and_hit(url: str, query: str) -> Tuple[str, bool, str]:
    try:
        html = _http_get(url, timeout=20)
    except Exception as e:
        return "", False, f"fetch_error: {e}"

    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    page_title = _strip_html(m.group(1)) if m else ""

    q = query.lower().strip()
    hit = q in html.lower() if q else False
    return page_title, hit, ""


def _search_web(query: str, num: int) -> Tuple[List[Dict], str]:
    if SERPAPI_KEY:
        return _search_serpapi(query, num), "serpapi"
    return _search_duckduckgo(query, num), "duckduckgo"


def run_byline_search(query: str, max_results: int) -> Dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("query er påkrævet")

    rows = _load_results()
    by_url = {str(r.get("url", "")).strip(): r for r in rows if r.get("url")}

    search_hits, source = _search_web(query, max_results)

    added = 0
    updated = 0
    confirmed = 0

    for hit in search_hits:
        url = hit["url"]
        page_title, is_confirmed, fetch_note = _fetch_page_title_and_hit(url, query)
        now_utc = _now_iso()

        base = {
            "url": url,
            "domain": hit.get("domain", ""),
            "google_title": hit.get("google_title", ""),
            "google_snippet": hit.get("google_snippet", ""),
            "query_byline": query,
            "confirmed_byline": query if is_confirmed else "",
            "page_title": page_title,
            "fetch_note": fetch_note,
            "search_date_raw": "",
            "search_date": hit.get("search_date", ""),
            "published_at_raw": hit.get("published_at_raw", ""),
            "published_at": "",
            "first_seen_utc": now_utc,
            "status": "confirmed_on_page" if is_confirmed else "searching",
        }

        if url in by_url:
            row = by_url[url]
            row["domain"] = base["domain"] or row.get("domain", "")
            row["google_title"] = base["google_title"] or row.get("google_title", "")
            row["google_snippet"] = base["google_snippet"] or row.get("google_snippet", "")
            row["query_byline"] = query
            row["page_title"] = base["page_title"] or row.get("page_title", "")
            row["fetch_note"] = base["fetch_note"]
            row["search_date"] = base["search_date"]
            row["published_at_raw"] = base["published_at_raw"]
            if is_confirmed:
                row["confirmed_byline"] = query
                row["status"] = "confirmed_on_page"
            else:
                row.setdefault("status", "searching")
            updated += 1
        else:
            rows.append(base)
            by_url[url] = base
            added += 1

        if is_confirmed:
            confirmed += 1

    rows.sort(key=lambda r: str(r.get("first_seen_utc", "")), reverse=True)
    _save_results(rows)

    return {
        "ok": True,
        "query": query,
        "source": source,
        "searched": len(search_hits),
        "added": added,
        "updated": updated,
        "confirmed": confirmed,
        "total": len(rows),
        "results_path": str(DATA_PATH),
    }


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _json_response(self, 200, {"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True, "service": "rsadm-byline", "time": _now_iso()})
            return

        if parsed.path in ("/api/byline/search", "/byline/run"):
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip()
            max_results = int((params.get("max_results", [str(DEFAULT_MAX_RESULTS)])[0] or DEFAULT_MAX_RESULTS))
            try:
                out = run_byline_search(query, max_results)
                _json_response(self, 200, out)
            except Exception as e:
                _json_response(self, 400, {"ok": False, "error": str(e)})
            return

        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/byline/search", "/byline/run"):
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            _json_response(self, 400, {"ok": False, "error": "invalid_json"})
            return

        query = str(body.get("query", "")).strip()
        max_results = int(body.get("max_results", DEFAULT_MAX_RESULTS))

        try:
            out = run_byline_search(query, max_results)
            _json_response(self, 200, out)
        except Exception as e:
            _json_response(self, 400, {"ok": False, "error": str(e)})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Byline API kører på http://localhost:{PORT}")
    print("POST /api/byline/search med JSON: {\"query\":\"RSFOTO\",\"max_results\":10}")
    if SERPAPI_KEY:
        print("Søgekilde: SerpAPI (Google)")
    else:
        print("Søgekilde: DuckDuckGo HTML (ingen API-nøgle sat)")
    if ALLOW_INSECURE_SSL:
        print("SSL-verify er slået fra (ALLOW_INSECURE_SSL=1). Brug kun hvis nødvendigt.")
    server.serve_forever()


if __name__ == "__main__":
    main()
