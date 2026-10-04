import json
import logging
import os
import time
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox
from curl_cffi import requests as curl
from flask import Response, stream_with_context

from cloudflare import is_blocked
from sessions import host

log = logging.getLogger("scraper")
proxy = os.environ.get("proxy")
CHALLENGE_WAIT_S = int(os.environ.get("CHALLENGE_WAIT_S", "30"))


class TransportError(Exception):
    """Nothing answered: dead proxy, dns or timeout. Never a block."""


def _camoufox_proxy():
    if not proxy:
        return None
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"], out["password"] = u.username, u.password or ""
    return out


def light_session(headers, cookies):
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


def fetch(url, headers, cookies, method="GET", data=None, follow=True):
    """Returns (payload_or_None, content_type). None means blocked."""
    log.info("Attempting light %s request to %s", method, url)
    with light_session(headers, cookies) as s:
        r = s.request(method, url, data=data, allow_redirects=follow)

    body = r.content.decode("utf-8", errors="ignore")
    resp_headers = dict(r.headers)
    content_type = resp_headers.get("content-type", "")
    if is_blocked(r.status_code, body):
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


def stream(url, headers, cookies):
    """Returns (flask_response_or_None, content_type). None means blocked."""
    s = light_session(headers, cookies)
    r = s.get(url, stream=True)
    content_type = r.headers.get("content-type", "application/octet-stream")

    def close():
        r.close()
        s.close()

    chunks = r.iter_content(chunk_size=8192)
    first_chunk = next(chunks, b"")
    if is_blocked(r.status_code, first_chunk.decode("utf-8", errors="ignore")):
        close()
        return None, content_type

    def generate():
        try:
            yield first_chunk
            yield from (c for c in chunks if c)
        finally:
            close()

    response = Response(
        stream_with_context(generate()), status=r.status_code, content_type=content_type
    )
    # Upstream bytes must never script against this origin.
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if disposition := r.headers.get("content-disposition"):
        response.headers["Content-Disposition"] = disposition
    return response, content_type


def run_stealth(url):
    """Returns (cookies, headers). geoip=True: Turnstile fails when locale/timezone
    mismatch the egress IP. The UA is returned because cf_clearance is bound to it.
    An unchallenged page yields a jar with no clearance in it."""
    log.info("[stealth] Starting run on %s.", url)
    with Camoufox(
        headless=True, humanize=True, os="windows", geoip=True, proxy=_camoufox_proxy()
    ) as browser:
        page = browser.new_page()
        nav = {"status": 200}

        def _note(resp):
            if resp.request.is_navigation_request() and resp.frame is page.main_frame:
                nav["status"] = resp.status

        page.on("response", _note)
        page.goto(url, wait_until="domcontentloaded")

        def challenged():
            try:
                return is_blocked(nav["status"], page.content())
            except Exception:  # page.content() raises mid-redirect
                return True

        deadline = time.monotonic() + CHALLENGE_WAIT_S
        while challenged() and time.monotonic() < deadline:
            time.sleep(1)
        if challenged():
            raise RuntimeError(f"challenge still present on {url}")

        cookies = {c["name"]: str(c["value"]) for c in page.context.cookies()}
        headers = {"user-agent": page.evaluate("navigator.userAgent")}
    return cookies, headers


def refresh_session(store, url, seen_cookies=None):
    """Solve `url` in a browser and store the jar under its host.
    Skips the run if a jar newer than `seen_cookies` is already stored, which happens
    when another thread finished while this one queued on the lock. None accepts any jar."""
    name = host(url)
    d = store.get(name)
    with d.stealth_lock:
        if d.cookies and d.cookies != seen_cookies:
            log.info("[stealth] %s session already refreshed, skipping run.", name)
            return
        d.cookies, d.headers = {}, {}
        d.cookies, d.headers = run_stealth(url)
        log.info("[stealth] Session stored for %s.", name)
