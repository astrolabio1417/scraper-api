import json
import os
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests as req
from flask import Flask, jsonify, request, Response, stream_with_context
from scrapling.fetchers import FetcherSession, StealthySession

app = Flask(__name__)

_session_store = {
    "sessions": {},  # { "domain": { "cookies": {}, "headers": {} } }
    "lock": threading.Lock(),
}

_stealth_locks = {}
_stealth_locks_lock = threading.Lock()

CLOUDFLARE_STATUS_CODES = {403, 503}
CLOUDFLARE_DOM_MARKERS = [
    "<title>just a moment...</title>",
    'id="cf-challenge-form"',
    "cf-browser-verification",
    'id="challenge-running"',
    'id="cf-please-wait"',
]


def _get_domain(url):
    return urlparse(url).netloc


def _get_root_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _get_stealth_lock(domain):
    with _stealth_locks_lock:
        if domain not in _stealth_locks:
            _stealth_locks[domain] = threading.Lock()
        return _stealth_locks[domain]


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
    if status_code in CLOUDFLARE_STATUS_CODES:
        return True
    lower = body.lower()
    return any(marker in lower for marker in CLOUDFLARE_DOM_MARKERS)


def _get_session(domain):
    with _session_store["lock"]:
        session = _session_store["sessions"].get(domain, {})
        return session.get("headers", {}).copy(), session.get("cookies", {}).copy()


def _clear_session(domain):
    with _session_store["lock"]:
        _session_store["sessions"].pop(domain, None)


def _run_stealth(url, extra_headers=None):
    """Run stealth on the domain root to harvest valid cookies."""
    domain = _get_domain(url)
    root_url = _get_root_url(url)

    print(f"[stealth] Starting stealth run for {domain} via {root_url}.")
    with StealthySession(headless=True, solve_cloudflare=True) as browser:
        kwargs = {}
        if extra_headers:
            kwargs["headers"] = extra_headers

        page = browser.fetch(root_url, stealthy_headers=True, **kwargs)

        cookies = {}
        if page.cookies:
            cookies = {c["name"]: str(c["value"]) for c in page.cookies}

        with _session_store["lock"]:
            _session_store["sessions"][domain] = {
                "cookies": cookies,
                "headers": page.request_headers,
            }
        print(f"[stealth] Session stored for {domain}.")


def _fetch_via_stealth(url, extra_headers=None):
    """
    Acquire per-domain lock and run stealth if no session exists yet.
    If another thread already refreshed the session while waiting,
    skip the run entirely.
    """
    domain = _get_domain(url)
    lock = _get_stealth_lock(domain)

    with lock:
        _, cookies = _get_session(domain)
        if cookies:
            print(f"[stealth] {domain} session already ready, skipping run.")
            return

        _clear_session(domain)
        try:
            _run_stealth(url, extra_headers)
        except Exception as exc:
            print(f"[stealth] Error for {domain}: {exc}")
            raise


def _fetch_via_session(url, headers, cookies):
    print("[session] Attempting light HTTP request...")
    with FetcherSession(impersonate="chrome", headers=headers) as session:
        page = session.get(url, stealthy_headers=True, cookies=cookies)

    body = page.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="ignore")

    if _is_blocked(page.status, body):
        return None, None

    parsed, is_json = _try_parse_json(body)
    return {
        "success": True,
        "status_code": page.status,
        "url": url,
        "is_json": is_json,
        "data": parsed if is_json else body,
    }, 200


def _stream_via_session(url, headers, cookies):
    s = req.Session()
    s.headers.update(headers)

    r = s.get(url, cookies=cookies, stream=True, timeout=30)

    first_chunk = next(r.iter_content(chunk_size=1024), b"")
    preview = first_chunk.decode("utf-8", errors="ignore")

    if _is_blocked(r.status_code, preview):
        r.close()
        return None

    def generate():
        yield first_chunk
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    content_type = r.headers.get("content-type", "application/octet-stream")
    content_disposition = r.headers.get("content-disposition", "")

    response = Response(
        stream_with_context(generate()),
        status=r.status_code,
        content_type=content_type,
    )
    if content_disposition:
        response.headers["Content-Disposition"] = content_disposition

    return response


@app.route("/api/fetch", methods=["POST"])
def handle_fetch():
    body = request.get_json(silent=True, force=True) or {}
    url = body.get("url")

    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    if body.get("params") and isinstance(body["params"], dict):
        url = _build_url_with_params(url, body["params"])

    domain = _get_domain(url)
    extra_headers = body.get("headers") or {}

    # Attempt 1: existing session
    cached_headers, cached_cookies = _get_session(domain)
    if extra_headers:
        cached_headers.update(extra_headers)

    if cached_cookies and cached_headers:
        try:
            result, status = _fetch_via_session(url, cached_headers, cached_cookies)
            if result is not None:
                print("[fetch] Session request succeeded.")
                return jsonify(result), status
            print("[fetch] Session blocked, refreshing via stealth.")
        except Exception as exc:
            print(f"[fetch] Session error: {exc}")

    # Attempt 2: stealth refresh, then retry session
    try:
        _fetch_via_stealth(url, extra_headers or None)
    except Exception as exc:
        return jsonify({"error": f"Stealth failed: {exc}"}), 502

    cached_headers, cached_cookies = _get_session(domain)
    if extra_headers:
        cached_headers.update(extra_headers)

    result, status = _fetch_via_session(url, cached_headers, cached_cookies)
    if result is not None:
        return jsonify(result), status

    return jsonify({"error": "Failed to fetch after stealth refresh"}), 502


@app.route("/api/download", methods=["GET"])
def handle_download():
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    params = request.args.to_dict()
    params.pop("url", None)

    if params:
        url = _build_url_with_params(url, params)

    domain = _get_domain(url)

    # Attempt 1: existing session
    cached_headers, cached_cookies = _get_session(domain)

    if cached_cookies and cached_headers:
        try:
            result = _stream_via_session(url, cached_headers, cached_cookies)

            if result is not None:
                print("[download] Session stream succeeded.")
                return result

            print("[download] Session blocked, refreshing via stealth.")
        except Exception as exc:
            print(f"[download] Session error: {exc}")

    # Attempt 2: stealth refresh, then retry stream
    try:
        _fetch_via_stealth(url)
    except Exception as exc:
        return jsonify({"error": f"Stealth failed: {exc}"}), 502

    cached_headers, cached_cookies = _get_session(domain)

    try:
        result = _stream_via_session(url, cached_headers, cached_cookies)

        if result is not None:
            print("[download] Session stream succeeded after stealth refresh.")
            return result
    except Exception as exc:
        print(f"[download] Final stream error: {exc}")

    return jsonify({"error": "Failed to download after stealth refresh"}), 502


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
