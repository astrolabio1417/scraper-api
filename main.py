import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from camoufox.sync_api import Camoufox
from curl_cffi import requests as curl
from flask import Flask, jsonify, request, Response, stream_with_context

app = Flask(__name__)
proxy = os.environ.get("proxy")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scraper")

STEALTH_COOLDOWN_S = int(os.environ.get("STEALTH_COOLDOWN_S", "900"))
CHALLENGE_WAIT_S = int(os.environ.get("CHALLENGE_WAIT_S", "30"))

CLOUDFLARE_STATUS_CODES = {503}
# Language-independent only: the interstitial is served in the egress IP's language,
# so its title is not a marker. _cf_chl_opt is emitted by the challenge page alone.
CLOUDFLARE_DOM_MARKERS = [
    "_cf_chl_opt",
    'id="cf-challenge-form"',
    "cf-browser-verification",
    'id="challenge-running"',
    'id="cf-please-wait"',
]
# Also injected into healthy 200 pages for fingerprinting; a challenge only with an error status.
CLOUDFLARE_ERROR_DOM_MARKERS = [
    "/cdn-cgi/challenge-platform/",
    "__cf$cv$params",
]


@dataclass
class Domain:
    """Cookies/headers live on the netloc record; the verdict on the status-key record."""

    cookies: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    plain_works: bool | None = None
    stealth_failed_at: float | None = None
    stealth_lock: threading.Lock = field(default_factory=threading.Lock)


_domains: dict[str, Domain] = {}
_lock = threading.Lock()


def _domain(key):
    with _lock:
        return _domains.setdefault(key, Domain())


def _get_domain(url):
    return urlparse(url).netloc


def _get_status_key(url):
    """Registrable domain, so sibling CDN hosts share one verdict and cooldown."""
    host = (urlparse(url).hostname or "").lower()
    labels = host.split(".")
    if len(labels) <= 2 or host.replace(".", "").isdigit():
        return host
    # ponytail: last two labels; co.uk-style suffixes yield a broader key, never a wrong one.
    return ".".join(labels[-2:])


def _in_stealth_cooldown(key):
    failed_at = _domain(key).stealth_failed_at
    return failed_at is not None and time.monotonic() - failed_at < STEALTH_COOLDOWN_S


def _set_domain_status(key, plain_works):
    d = _domain(key)
    d.plain_works = plain_works
    if plain_works:
        d.stealth_failed_at = None


def _build_url_with_params(base_url, params):
    parts = list(urlparse(base_url))
    query = dict(parse_qsl(parts[4]))
    query.update(params)
    parts[4] = urlencode(query)
    return urlunparse(parts)


def _is_blocked(status_code, body):
    """A bare 403 is an auth denial, not a challenge; it escalates only with a marker."""
    lower = (body or "").lower()
    if any(m in lower for m in CLOUDFLARE_DOM_MARKERS):
        return True
    if status_code >= 400 and any(m in lower for m in CLOUDFLARE_ERROR_DOM_MARKERS):
        return True
    return status_code in CLOUDFLARE_STATUS_CODES


def _camoufox_proxy():
    if not proxy:
        return None
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"], out["password"] = u.username, u.password or ""
    return out


def _run_stealth(url, use_root=True):
    """geoip=True: Turnstile fails when locale/timezone mismatch the egress IP.
    The UA is stored because cf_clearance is bound to it."""
    parsed = urlparse(url)
    root_url = f"{parsed.scheme}://{parsed.netloc}/" if use_root else url

    log.info("[stealth] Starting run for %s via %s.", parsed.netloc, root_url)
    with Camoufox(
        headless=True, humanize=True, os="windows", geoip=True, proxy=_camoufox_proxy()
    ) as browser:
        page = browser.new_page()
        nav = {"status": 200}

        def _note(resp):
            if resp.request.is_navigation_request() and resp.frame is page.main_frame:
                nav["status"] = resp.status

        page.on("response", _note)
        page.goto(root_url, wait_until="domcontentloaded")

        def challenged():
            try:
                return _is_blocked(nav["status"], page.content())
            except Exception:  # page.content() raises mid-redirect
                return True

        deadline = time.monotonic() + CHALLENGE_WAIT_S
        while challenged() and time.monotonic() < deadline:
            time.sleep(1)
        if challenged():
            raise RuntimeError(f"challenge still present on {root_url}")

        cookies = {c["name"]: str(c["value"]) for c in page.context.cookies()}
        headers = {"user-agent": page.evaluate("navigator.userAgent")}

    d = _domain(parsed.netloc)
    d.cookies, d.headers = cookies, headers
    log.info("[stealth] Session stored for %s.", parsed.netloc)


def _fetch_via_stealth(url, use_root=True, seen_cookies=None):
    """Skip the run if another thread stored a session newer than `seen_cookies`
    while this one queued on the lock."""
    d = _domain(_get_domain(url))
    with d.stealth_lock:
        if d.cookies and (use_root or d.cookies != seen_cookies):
            log.info(
                "[stealth] %s session already refreshed, skipping run.",
                _get_domain(url),
            )
            return
        d.cookies, d.headers = {}, {}
        _run_stealth(url, use_root=use_root)


def _light_session(headers, cookies):
    """Firefox impersonation: Cloudflare fingerprints TLS against the Camoufox UA.
    socks5h resolves DNS at the proxy; socks5 resolves locally and fails."""
    curl_proxy = proxy.replace("socks5://", "socks5h://", 1) if proxy else None
    return curl.Session(
        impersonate="firefox",
        headers=headers,
        cookies=cookies,
        proxy=curl_proxy,
        timeout=30,
    )


def _fetch_via_session(url, headers, cookies, method="GET", data=None, follow=True):
    """Returns (payload_or_None, content_type). None means blocked."""
    log.info("Attempting light %s request to %s", method, url)
    with _light_session(headers, cookies) as s:
        r = s.request(method, url, data=data, allow_redirects=follow)

    body = r.content.decode("utf-8", errors="ignore")
    resp_headers = dict(r.headers)
    content_type = resp_headers.get("content-type", "")
    if _is_blocked(r.status_code, body):
        return None, content_type

    try:
        data, is_json = json.loads(body), True
    except ValueError:
        data, is_json = body, False
    return {
        "success": True,
        "status_code": r.status_code,
        "url": url,
        "headers": resp_headers,
        "location": resp_headers.get("location"),
        "is_json": is_json,
        "data": data,
    }, content_type


def _stream_via_session(url, headers, cookies):
    """Returns (flask_response_or_None, content_type). None means blocked."""
    r = _light_session(headers, cookies).get(url, stream=True)
    content_type = r.headers.get("content-type", "application/octet-stream")

    stream = r.iter_content(chunk_size=8192)
    first_chunk = next(stream, b"")
    if _is_blocked(r.status_code, first_chunk.decode("utf-8", errors="ignore")):
        r.close()
        return None, content_type

    def generate():
        try:
            yield first_chunk
            yield from (c for c in stream if c)
        finally:
            r.close()

    response = Response(
        stream_with_context(generate()), status=r.status_code, content_type=content_type
    )
    if disposition := r.headers.get("content-disposition"):
        response.headers["Content-Disposition"] = disposition
    return response, content_type


def _with_escalation(url, attempt, extra_headers=None, target_stealth_ok=None, tag=""):
    """Run `attempt(headers, cookies)` plain, then after stealth on the root, then on the URL.
    `attempt` returns (value, content_type); None means blocked.
    `target_stealth_ok(content_type)` gates the last step. Returns (value, error)."""
    host, key = _get_domain(url), _get_status_key(url)

    def current_session():
        d = _domain(host)
        headers, cookies = d.headers.copy(), d.cookies.copy()
        if extra_headers:
            session_ua = headers.get("user-agent")
            headers.update(extra_headers)
            if session_ua:  # cf_clearance is bound to the solving browser's UA
                headers = {
                    k: v for k, v in headers.items() if k.lower() != "user-agent"
                }
                headers["user-agent"] = session_ua
        return headers, cookies

    def try_attempt(stage):
        try:
            return attempt(*current_session())
        except Exception as exc:
            log.warning("[%s] %s attempt failed: %s", tag, stage, exc)
            return None, ""

    headers, cookies = current_session()
    used_cached_session = bool(headers and cookies)

    if _domain(key).plain_works is not False or used_cached_session:
        value, _ = try_attempt("plain")
        if value is not None:
            log.info("[%s] Succeeded without stealth.", tag)
            _set_domain_status(key, not used_cached_session)
            return value, None

    _set_domain_status(key, False)
    if _in_stealth_cooldown(key):
        log.info("[%s] %s in stealth cooldown, not launching a browser.", tag, key)
        return None, f"Blocked; stealth recently failed for {key}"

    for use_root in (True, False):
        where = "domain root" if use_root else "target URL"
        try:
            _fetch_via_stealth(url, use_root=use_root, seen_cookies=cookies)
        except Exception as exc:
            log.warning("[%s] Stealth on %s failed: %s", tag, where, exc)
            continue

        headers, cookies = current_session()
        value, content_type = try_attempt(f"after stealth on {where}")
        if value is not None:
            log.info("[%s] Succeeded after stealth on %s.", tag, where)
            return value, None
        if use_root and target_stealth_ok and not target_stealth_ok(content_type):
            log.info("[%s] Skipping target-URL stealth (%s).", tag, content_type)
            break

    _domain(key).stealth_failed_at = time.monotonic()
    return None, "Failed after stealth refresh"


@app.route("/api/fetch", methods=["POST"])
def handle_fetch():
    body = request.get_json(silent=True, force=True) or {}
    url = body.get("url")
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    if isinstance(body.get("params"), dict) and body["params"]:
        url = _build_url_with_params(url, body["params"])

    method = (body.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return jsonify({"error": f"Unsupported method: {method}"}), 400

    result, error = _with_escalation(
        url,
        lambda headers, cookies: _fetch_via_session(
            url,
            headers,
            cookies,
            method=method,
            data=body.get("data"),
            follow=body.get("follow_redirects", True),
        ),
        extra_headers=body.get("headers") or {},
        tag="fetch",
    )
    if error:
        return jsonify({"error": error}), 502
    return jsonify(result), 200


@app.route("/api/download", methods=["GET"])
def handle_download():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    params = request.args.to_dict()
    params.pop("url", None)
    if params:
        url = _build_url_with_params(url, params)

    result, error = _with_escalation(
        url,
        lambda headers, cookies: _stream_via_session(url, headers, cookies),
        target_stealth_ok=lambda ctype: ctype.startswith("text/html"),
        tag="download",
    )
    if error:
        return jsonify({"error": error}), 502
    return result


@app.route("/api/session", methods=["GET"])
def handle_session():
    domain = request.args.get("domain")
    with _lock:
        sessions = {
            k: {"cookies": d.cookies, "headers": d.headers}
            for k, d in _domains.items()
            if d.cookies
        }
    if domain:
        if domain not in sessions:
            return jsonify({"error": f"No session for {domain}"}), 404
        return jsonify(sessions[domain]), 200
    if not sessions:
        return jsonify({"error": "No sessions available yet"}), 404
    return jsonify(sessions), 200


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        threaded=True,
    )
