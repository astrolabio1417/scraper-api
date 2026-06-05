import json
import os
import queue
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from flask import Flask, jsonify, request
from scrapling.fetchers import FetcherSession, StealthySession

app = Flask(__name__)

_task_queue = queue.Queue()
_session_store = {
    "cookies": {},
    "headers": {},
    "lock": threading.Lock(),
}

CLOUDFLARE_STATUS_CODES = {403, 503}
CLOUDFLARE_DOM_MARKERS = [
    "<title>just a moment...</title>",
    'id="cf-challenge-form"',
    "cf-browser-verification",
    'id="challenge-running"',
    'id="cf-please-wait"',
]


def _stealth_worker():
    print("Stealth worker ready.")
    while True:
        task = _task_queue.get()
        if task is None:
            break

        url, fetch_kwargs, reply = task

        try:
            print(f"[stealth] Launching browser for: {url}")
            with StealthySession(headless=True, solve_cloudflare=True) as browser:
                page = browser.fetch(url, stealthy_headers=True, **fetch_kwargs)

                cookies = {}
                if page.cookies:
                    cookies = {c["name"]: str(c["value"]) for c in page.cookies}

                with _session_store["lock"]:
                    _session_store["cookies"] = cookies
                    _session_store["headers"] = page.request_headers

                body = page.body

                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="ignore")

                parsed, is_json = _try_parse_json(body)

                reply.put(
                    (
                        {
                            "success": True,
                            "status_code": getattr(page, "status", 200),
                            "url": page.url,
                            "is_json": is_json,
                            "data": parsed if is_json else body,
                        },
                        200,
                    )
                )

        except Exception as exc:
            print(f"[stealth] Error: {exc}")
            reply.put(({"success": False, "error": str(exc)}, 500))
        finally:
            _task_queue.task_done()


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


def _fetch_via_stealth(url, extra_headers=None):
    reply = queue.Queue()
    kwargs = {}
    if extra_headers:
        kwargs["headers"] = extra_headers
    _task_queue.put((url, kwargs, reply))
    return reply.get()


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


@app.route("/api/fetch", methods=["POST"])
def handle_fetch():
    body = request.get_json() or {}
    url = body.get("url")

    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    if body.get("params") and isinstance(body["params"], dict):
        url = _build_url_with_params(url, body["params"])

    with _session_store["lock"]:
        cached_headers = _session_store["headers"].copy()
        cached_cookies = _session_store["cookies"].copy()

    extra_headers = body.get("headers") or {}
    if extra_headers:
        cached_headers.update(extra_headers)

    if cached_cookies and cached_headers:
        try:
            result, status = _fetch_via_session(url, cached_headers, cached_cookies)
            if result is not None:
                print("[session] Request succeeded.")
                return jsonify(result), status
            print("[session] Challenge detected, falling back to stealth.")
        except Exception as exc:
            print(f"[session] Error: {exc}")

    result, status = _fetch_via_stealth(url, extra_headers or None)
    return jsonify(result), status


_worker_thread = threading.Thread(target=_stealth_worker, daemon=True)
_worker_thread.start()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5001, debug=debug)
