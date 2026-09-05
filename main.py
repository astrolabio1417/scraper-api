import json
import logging
import os
import threading
import time
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

_session_store = {
    "sessions": {},  # { "domain": { "cookies": {}, "headers": {} } }
    "lock": threading.Lock(),
}

_stealth_locks = {}
_stealth_locks_lock = threading.Lock()

# Keyed by _get_status_key, not by host.
# _domain_status: True = plain requests work (no Cloudflare), False = needs stealth
# _stealth_failures: when a stealth run last failed to unblock a domain
_domain_status = {}
_stealth_failures = {}
_domain_status_lock = threading.Lock()

# After stealth fails to unblock a domain, stop launching browsers at it for this long.
STEALTH_COOLDOWN_S = int(os.environ.get("STEALTH_COOLDOWN_S", "900"))

CHALLENGE_WAIT_S = int(os.environ.get("CHALLENGE_WAIT_S", "30"))

# 503 is Cloudflare's classic interstitial — always worth a stealth run.
CLOUDFLARE_STATUS_CODES = {503}
# A 403 alone is just an auth denial; it escalates only with one of these markers.
CLOUDFLARE_DOM_MARKERS = [
    "<title>just a moment...</title>",
    'id="cf-challenge-form"',
    "cf-browser-verification",
    'id="challenge-running"',
    'id="cf-please-wait"',
]


def _get_domain(url):
    return urlparse(url).netloc


def _get_status_key(url):
    """
    Key for the per-domain verdict and cooldown — deliberately broader than the
    cookie key, so sibling CDN hosts (vault-05/vault-06.example.net) share one
    verdict instead of each relearning it with a browser launch.
    """
    host = (urlparse(url).hostname or "").lower()
    labels = host.split(".")
    # IPv4 literals have no registrable domain; grouping by last two octets would collide.
    if len(labels) <= 2 or host.replace(".", "").isdigit():
        return host
    # ponytail: naive last-two-labels rule. For co.uk-style suffixes this just
    # yields a broader key (a shared verdict), never a wrong host match.
    return ".".join(labels[-2:])


def _get_root_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _get_stealth_lock(domain):
    with _stealth_locks_lock:
        if domain not in _stealth_locks:
            _stealth_locks[domain] = threading.Lock()
        return _stealth_locks[domain]


def _get_domain_status(domain):
    """Returns True (plain works), False (needs stealth), or None (unknown)."""
    with _domain_status_lock:
        return _domain_status.get(domain)


def _set_domain_status(domain, plain_works):
    with _domain_status_lock:
        _domain_status[domain] = plain_works
        if plain_works:
            _stealth_failures.pop(domain, None)


def _in_stealth_cooldown(key):
    """True when stealth recently failed for this domain — don't launch a browser."""
    with _domain_status_lock:
        failed_at = _stealth_failures.get(key)
    return failed_at is not None and (time.monotonic() - failed_at) < STEALTH_COOLDOWN_S


def _mark_stealth_failed(key):
    with _domain_status_lock:
        _stealth_failures[key] = time.monotonic()


def _try_parse_json(text):
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


def _build_url_with_params(base_url, params):
    parts = list(urlparse(base_url))
    query = dict(parse_qsl(parts[4]))
    query.update(params)
    parts[4] = urlencode(query)
    return urlunparse(parts)


def _is_blocked(status_code, body):
    """
    True when a stealth run could plausibly help: a 503 interstitial, or a body
    carrying a challenge marker. A bare 403 is returned to the caller instead of
    burning two browser launches on a plain authorization denial.
    """
    lower = (body or "").lower()
    if any(marker in lower for marker in CLOUDFLARE_DOM_MARKERS):
        return True
    return status_code in CLOUDFLARE_STATUS_CODES


def _get_session(domain):
    with _session_store["lock"]:
        session = _session_store["sessions"].get(domain, {})
        return session.get("headers", {}).copy(), session.get("cookies", {}).copy()


def _clear_session(domain):
    with _session_store["lock"]:
        _session_store["sessions"].pop(domain, None)


def _camoufox_proxy():
    """Translate the proxy URL env into Playwright's proxy dict."""
    if not proxy:
        return None
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"], out["password"] = u.username, u.password or ""
    return out


def _run_stealth(url, use_root=True):
    """
    Solve the challenge in Camoufox and store its cookies plus user agent.

    geoip=True is required: Turnstile fails when locale/timezone don't match the
    egress IP. The UA is stored because cf_clearance is bound to it.
    """
    domain = _get_domain(url)
    root_url = _get_root_url(url) if use_root else url

    log.info("[stealth] Starting run for %s via %s.", domain, root_url)
    with Camoufox(
        headless=True, humanize=True, os="windows", geoip=True, proxy=_camoufox_proxy()
    ) as browser:
        page = browser.new_page()
        page.goto(root_url, wait_until="domcontentloaded")

        def challenged():
            try:
                return _is_blocked(200, page.content())
            except Exception:
                # page.content() raises while the challenge redirects; still blocked.
                return True

        deadline = time.monotonic() + CHALLENGE_WAIT_S
        while challenged() and time.monotonic() < deadline:
            time.sleep(1)

        if challenged():
            raise RuntimeError(f"challenge still present on {root_url}")

        cookies = {c["name"]: str(c["value"]) for c in page.context.cookies()}
        headers = {"user-agent": page.evaluate("navigator.userAgent")}

    with _session_store["lock"]:
        _session_store["sessions"][domain] = {"cookies": cookies, "headers": headers}
    log.info("[stealth] Session stored for %s.", domain)


def _fetch_via_stealth(url, use_root=True):
    """
    Acquire per-domain lock and run stealth if no session exists yet.
    If another thread already refreshed the session while waiting,
    skip the run entirely. use_root=False forces a run against the url itself
    (for pages that challenge even when the homepage doesn't).
    """
    domain = _get_domain(url)
    lock = _get_stealth_lock(domain)

    with lock:
        _, cookies = _get_session(domain)
        if cookies and use_root:
            log.info("[stealth] %s session already ready, skipping run.", domain)
            return

        _clear_session(domain)
        try:
            _run_stealth(url, use_root=use_root)
        except Exception as exc:
            log.error("[stealth] Error for %s: %s", domain, exc)
            raise


def _light_session(headers, cookies):
    """
    Impersonate Firefox so the TLS fingerprint matches the Camoufox UA and cookie;
    Cloudflare fingerprints TLS, and a Chrome handshake under a Firefox UA stands out.
    """
    # socks5h resolves DNS at the proxy, matching Firefox; socks5 resolves locally and fails.
    curl_proxy = proxy.replace("socks5://", "socks5h://", 1) if proxy else None
    return curl.Session(
        impersonate="firefox", headers=headers, cookies=cookies, proxy=curl_proxy, timeout=30
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

    parsed, is_json = _try_parse_json(body)
    return {
        "success": True,
        "status_code": r.status_code,
        "url": url,
        "headers": resp_headers,
        "location": resp_headers.get("location"),
        "is_json": is_json,
        "data": parsed if is_json else body,
    }, content_type


def _stream_via_session(url, headers, cookies):
    """Returns (flask_response_or_None, content_type_of_the_response)."""
    r = _light_session(headers, cookies).get(url, stream=True)

    content_type = r.headers.get("content-type", "application/octet-stream")

    stream = r.iter_content(chunk_size=8192)
    first_chunk = next(stream, b"")
    preview = first_chunk.decode("utf-8", errors="ignore")

    if _is_blocked(r.status_code, preview):
        r.close()
        return None, content_type

    def generate():
        try:
            yield first_chunk
            for chunk in stream:
                if chunk:
                    yield chunk
        finally:
            r.close()  # also runs if the client disconnects mid-stream

    content_disposition = r.headers.get("content-disposition", "")

    response = Response(
        stream_with_context(generate()),
        status=r.status_code,
        content_type=content_type,
    )
    if content_disposition:
        response.headers["Content-Disposition"] = content_disposition

    return response, content_type


def _should_try_plain(key, has_session):
    """Domains known to need stealth only get a plain attempt when cookies are cached."""
    return _get_domain_status(key) is not False or has_session


def _with_escalation(url, attempt, extra_headers=None, target_stealth_ok=None, tag=""):
    """
    Run `attempt(headers, cookies)` against progressively stronger sessions:
    cached/plain -> stealth on the domain root -> stealth on the URL itself.

    `attempt` returns (value, content_type); a value of None means blocked.
    `target_stealth_ok(content_type)` gates the final escalation — return False
    to skip pointing a browser at something it can't help with (e.g. a binary).

    Returns (value, None) on success, or (None, error_message).
    """
    domain = _get_domain(url)
    key = _get_status_key(url)

    def current_session():
        headers, cookies = _get_session(domain)
        if extra_headers:
            session_ua = headers.get("user-agent")
            headers.update(extra_headers)
            if session_ua:
                # cf_clearance is bound to the solving browser's UA, so a
                # caller-supplied User-Agent must not override it.
                headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
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

    if _should_try_plain(key, used_cached_session):
        value, _ = try_attempt("plain")
        if value is not None:
            log.info("[%s] Succeeded without stealth.", tag)
            # Only a success without cached cookies proves the domain works plain.
            _set_domain_status(key, not used_cached_session)
            return value, None

    _set_domain_status(key, False)

    if _in_stealth_cooldown(key):
        log.info("[%s] %s in stealth cooldown, not launching a browser.", tag, key)
        return None, f"Blocked; stealth recently failed for {key}"

    for use_root in (True, False):
        where = "domain root" if use_root else "target URL"
        try:
            _fetch_via_stealth(url, use_root=use_root)
        except Exception as exc:
            log.warning("[%s] Stealth on %s failed: %s", tag, where, exc)
            continue

        value, content_type = try_attempt(f"after stealth on {where}")
        if value is not None:
            log.info("[%s] Succeeded after stealth on %s.", tag, where)
            return value, None

        if use_root and target_stealth_ok and not target_stealth_ok(content_type):
            log.info("[%s] Skipping target-URL stealth (%s).", tag, content_type)
            break

    # Stealth couldn't get through; cool down to stop relaunching browsers.
    _mark_stealth_failed(key)
    return None, "Failed after stealth refresh"


@app.route("/api/fetch", methods=["POST"])
def handle_fetch():
    body = request.get_json(silent=True, force=True) or {}
    url = body.get("url")

    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    if body.get("params") and isinstance(body["params"], dict):
        url = _build_url_with_params(url, body["params"])

    extra_headers = body.get("headers") or {}
    method = (body.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return jsonify({"error": f"Unsupported method: {method}"}), 400

    req_opts = {
        "method": method,
        "data": body.get("data"),
        "follow": body.get("follow_redirects", True),
    }

    result, error = _with_escalation(
        url,
        lambda headers, cookies: _fetch_via_session(
            url, headers, cookies, **req_opts
        ),
        extra_headers=extra_headers,
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
        # Pointing a browser at a multi-GB binary buys nothing and burns the
        # domain lock for the full browser timeout.
        target_stealth_ok=lambda ctype: ctype.startswith("text/html"),
        tag="download",
    )
    if error:
        return jsonify({"error": error}), 502
    return result


@app.route("/api/session", methods=["GET"])
def handle_session():
    domain = request.args.get("domain")
    with _session_store["lock"]:
        if domain:
            session = _session_store["sessions"].get(domain)
            if not session:
                return jsonify({"error": f"No session for {domain}"}), 404
            return jsonify(session), 200
        if not _session_store["sessions"]:
            return jsonify({"error": "No sessions available yet"}), 404
        return jsonify(_session_store["sessions"]), 200


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5001, debug=debug, threaded=True)
