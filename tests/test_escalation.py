import pytest

import transport
from escalation import FetchRequest, with_escalation
from sessions import SessionStore


def ok(status=200):
    return {"success": True, "status_code": status}, "text/html"


def test_a_stateless_host_never_sends_a_jar(monkeypatch):
    """host-a needs no clearance, so a stored app session only buys a tighter
    rate-limit key. One attempt, no cookies, even when a session is sitting there."""
    store = SessionStore()
    d = store.get("host-a.example")
    d.cookies, d.headers = {"app_session": "stored"}, {"user-agent": "ua"}
    monkeypatch.setattr(SessionStore, "in_cooldown", lambda self, key: True)
    seen = []

    def attempt(headers, cookies):
        seen.append((headers, cookies))
        return ok(429) if cookies else ok(200)

    req = FetchRequest(url="https://host-a.example/e/abc", headers={"referer": "r"})
    value, err = with_escalation(store, req, attempt, tag="fetch")

    assert err is None and value["status_code"] == 200
    assert seen == [({"referer": "r"}, {})], "one attempt, caller headers, no jar"
    assert store.get("host-a.example").plain_works is not False


def test_a_normal_host_still_uses_its_session(monkeypatch):
    store = SessionStore()
    d = store.get("host-b.example")
    d.cookies, d.headers = {"cf_clearance": "solved"}, {"user-agent": "ua"}
    monkeypatch.setattr(SessionStore, "in_cooldown", lambda self, key: True)
    seen = []

    def attempt(headers, cookies):
        seen.append(cookies)
        return ok()

    with_escalation(store, FetchRequest(url="https://host-b.example/api"), attempt, tag="fetch")
    assert seen == [{"cf_clearance": "solved"}]


def test_app_cookies_are_not_replayed_but_clearance_is(monkeypatch):
    store = SessionStore()
    d = store.get("host-a.example")
    d.cookies = {"app_session": "s", "srv": "s0", "cf_clearance": "good"}
    d.headers = {"user-agent": "ua"}
    monkeypatch.setattr(SessionStore, "in_cooldown", lambda self, key: True)
    seen = []

    def attempt(headers, cookies):
        seen.append(cookies)
        return ok()

    with_escalation(store, FetchRequest(url="https://host-a.example/f/abc"), attempt, tag="fetch")
    assert seen == [{"cf_clearance": "good"}]


def test_session_ua_beats_caller_ua():
    store = SessionStore()
    d = store.get("walled.example")
    d.cookies, d.headers = {"cf_clearance": "good"}, {"user-agent": "Firefox/152.0"}
    seen = []

    def attempt(headers, cookies):
        seen.append(headers)
        return ok()

    req = FetchRequest(
        url="https://walled.example/p", headers={"User-Agent": "Chrome/146", "referer": "x"}
    )
    with_escalation(store, req, attempt)
    assert seen[0]["user-agent"] == "Firefox/152.0"
    assert "User-Agent" not in seen[0] and seen[0]["referer"] == "x"


def test_the_stored_jar_survives_a_caller_cookie():
    """`all` used to hand out the live dict, so a caller cookie rewrote the jar."""
    store = SessionStore()
    store.get("shop.example").cookies = {"session": "stored"}
    req = FetchRequest(
        url="https://shop.example/cart", session_cookies="all", cookies={"session": "caller"}
    )
    with_escalation(store, req, lambda h, c: ok())
    assert store.get("shop.example").cookies == {"session": "stored"}


def test_download_style_gate_skips_target_stealth_for_non_html(monkeypatch):
    store = SessionStore()
    ran = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: ran.append(k))
    store.set_status("big.example", False)

    value, err = with_escalation(
        store,
        FetchRequest(url="https://big.example/2gb.iso"),
        lambda h, c: (None, "application/octet-stream"),
        target_stealth_ok=lambda ctype: ctype.startswith("text/html"),
    )
    assert value is None and err
    assert len(ran) == 1, "the root run is fine; the browser must not open the binary"


@pytest.mark.parametrize(
    "body, message",
    [
        ({}, "Missing 'url'"),
        ({"url": "https://x/", "method": "DELETE"}, "Unsupported method"),
        ({"url": "https://x/", "headers": "nope"}, "'headers' must be an object"),
        ({"url": "https://x/", "cookies": ["a"]}, "'cookies' must be an object"),
        ({"url": "https://x/", "session_cookies": "some"}, "'session_cookies'"),
        ({"url": "https://x/", "method": 1}, "Unsupported method"),
        ({"url": "https://x/", "challenge_url": 5}, "'challenge_url'"),
        ({"url": "https://x/", "challenge_url": "https://y/"}, "'challenge_url'"),
    ],
)
def test_from_body_rejects_bad_input(body, message):
    with pytest.raises(ValueError, match=message):
        FetchRequest.from_body(body)


def test_from_body_applies_params():
    req = FetchRequest.from_body({"url": "https://x/p?a=1", "params": {"b": 2}})
    assert req.url == "https://x/p?a=1&b=2"
