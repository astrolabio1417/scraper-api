import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import transport
from sessions import host, pick_cookies, status_key
from transport import TransportError

log = logging.getLogger("scraper")


def add_params(base_url, params):
    parts = list(urlparse(base_url))
    query = dict(parse_qsl(parts[4]))
    query.update(params)
    parts[4] = urlencode(query)
    return urlunparse(parts)


@dataclass
class FetchRequest:
    url: str
    method: str = "GET"
    data: object = None
    follow_redirects: bool = True
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    session_cookies: str = "cf"
    challenge_url: str | None = None

    @classmethod
    def from_body(cls, body):
        """Raises ValueError with a message fit for a 400."""
        url = body.get("url")
        if not url or not isinstance(url, str):
            raise ValueError("Missing 'url' parameter")
        for key in ("params", "headers", "cookies"):
            if body.get(key) is not None and not isinstance(body[key], dict):
                raise ValueError(f"'{key}' must be an object")
        if body.get("params"):
            url = add_params(url, body["params"])
        method = body.get("method") or "GET"
        if not isinstance(method, str) or method.upper() not in ("GET", "POST"):
            raise ValueError(f"Unsupported method: {method}")
        method = method.upper()
        session_cookies = body.get("session_cookies", "cf")
        if session_cookies not in ("cf", "all"):
            raise ValueError("'session_cookies' must be 'cf' or 'all'")
        challenge_url = body.get("challenge_url") or None
        if challenge_url is not None and (
            not isinstance(challenge_url, str) or host(challenge_url) != host(url)
        ):
            raise ValueError("'challenge_url' must be a URL on the same host as 'url'")
        return cls(
            url=url,
            method=method,
            data=body.get("data"),
            follow_redirects=bool(body.get("follow_redirects", True)),
            headers=body.get("headers") or {},
            cookies=body.get("cookies") or {},
            session_cookies=session_cookies,
            challenge_url=challenge_url,
        )


def with_escalation(store, req, attempt, target_stealth_ok=None, tag=""):
    """Run `attempt(headers, cookies)` plain, then after stealth on the root, then on the URL.
    `attempt` returns (value, content_type); None means blocked.
    `target_stealth_ok(content_type)` gates the last step. Returns (value, error).
    `req.challenge_url` replaces both stealth passes with one run against it."""
    name, key = host(req.url), status_key(req.url)

    def current_session():
        d = store.get(name)
        cookies = pick_cookies(d.cookies, req.session_cookies)
        headers = d.headers.copy() if cookies else {}  # no jar, no fingerprint
        cookies.update(req.cookies)
        if req.headers:
            session_ua = headers.get("user-agent")
            headers.update(req.headers)
            if session_ua:  # cf_clearance is bound to the solving browser's UA
                headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
                headers["user-agent"] = session_ua
        return headers, cookies

    def try_attempt(stage):
        try:
            return attempt(*current_session())
        except Exception as exc:
            log.warning("[%s] %s attempt failed: %s", tag, stage, exc)
            raise TransportError(exc) from exc

    # Captured before the plain attempt: a jar another thread stores meanwhile must count as newer.
    seen = store.get(name).cookies.copy()
    used_cached_session = bool(pick_cookies(seen, req.session_cookies))

    if store.get(key).plain_works is not False or used_cached_session:
        try:
            value, _ = try_attempt("plain")
        except TransportError as exc:
            return None, f"Upstream unreachable: {exc}"
        if value is not None:
            log.info("[%s] Succeeded without stealth.", tag)
            store.set_status(key, not used_cached_session)
            return value, None

    store.set_status(key, False)
    if store.in_cooldown(key):
        log.info("[%s] %s in stealth cooldown, not launching a browser.", tag, key)
        return None, f"Blocked; stealth recently failed for {key}"

    root = f"{urlparse(req.url).scheme}://{name}/"
    passes = [req.challenge_url] if req.challenge_url else [root, req.url]

    for visit in passes:
        # The root pass reuses any stored jar; later passes only one newer than they failed with.
        stale = None if visit is root else seen
        try:
            transport.refresh_session(store, visit, seen_cookies=stale)
        except Exception as exc:
            log.warning("[%s] Stealth on %s failed: %s", tag, visit, exc)
            continue

        seen = store.get(name).cookies.copy()
        try:
            value, content_type = try_attempt(f"after stealth on {visit}")
        except TransportError as exc:
            return None, f"Upstream unreachable: {exc}"
        if value is not None:
            log.info("[%s] Succeeded after stealth on %s.", tag, visit)
            return value, None
        if visit is root and target_stealth_ok and not target_stealth_ok(content_type):
            log.info("[%s] Skipping target-URL stealth (%s).", tag, content_type)
            break

    store.get(key).stealth_failed_at = time.monotonic()
    return None, "Failed after stealth refresh"
