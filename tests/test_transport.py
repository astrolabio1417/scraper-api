import threading
import time
from types import SimpleNamespace

import pytest

import transport
from sessions import SessionStore
from transport import refresh_session, run_stealth


def test_concurrent_stealth_uses_a_session_another_thread_just_stored(monkeypatch):
    """A host whose root always challenges needs a target-URL run on every request.
    Without a dedupe, each concurrent caller launches its own browser and
    overwrites the session the previous one stored."""
    store = SessionStore()
    d = store.get("host-a.example")
    d.cookies, d.headers = {"cf_clearance": "fresh"}, {"user-agent": "ua"}
    runs = []
    monkeypatch.setattr(
        transport,
        "run_stealth",
        lambda url: (runs.append(url), ({}, {}))[1],
    )

    refresh_session(
        store, "https://host-a.example/e/abc", seen_cookies={"cf_clearance": "stale"}
    )
    assert runs == [], "should have reused the fresher session"

    refresh_session(
        store, "https://host-a.example/e/abc", seen_cookies={"cf_clearance": "fresh"}
    )
    assert runs == ["https://host-a.example/e/abc"], "a genuinely stale session must re-run"


def test_stealth_does_not_store_unsolved_challenge(fake_stealth):
    fake_stealth('<html><div id="cf-challenge-form">', [{"name": "cf_clearance", "value": "junk"}])
    store = SessionStore()
    with pytest.raises(RuntimeError):
        refresh_session(store, "https://walled.example/p")
    assert store.get("walled.example").cookies == {}


def test_stealth_stores_cookies_and_browser_ua(fake_stealth):
    fake_stealth("<html>ok</html>", [{"name": "cf_clearance", "value": "good"}])
    store = SessionStore()
    refresh_session(store, "https://walled.example/p")
    d = store.get("walled.example")
    assert (d.cookies, d.headers) == ({"cf_clearance": "good"}, {"user-agent": "Chrome/150.0"})


def test_localised_challenge_is_not_stored_as_a_solved_session(fake_stealth):
    """Cloudflare serves the interstitial in the egress IP's language, so the
    English title never matches from a JP exit."""
    jp = (
        "<html><head><title>しばらくお待ちください..."
        '</title></head><body><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1">'
        "</script></body></html>"
    )
    fake_stealth(jp, [{"name": "cf_clearance", "value": "junk"}], status=403)
    store = SessionStore()
    with pytest.raises(RuntimeError):
        refresh_session(store, "https://walled.example/p")
    assert store.get("walled.example").cookies == {}


def test_root_pass_reuses_any_stored_jar(monkeypatch):
    store = SessionStore()
    store.get("host-a.example").cookies = {"cf_clearance": "whatever"}
    runs = []
    monkeypatch.setattr(transport, "run_stealth", lambda url: (runs.append(url), ({}, {}))[1])
    refresh_session(store, "https://host-a.example/", seen_cookies=None)
    assert runs == []


def test_stealth_opens_exactly_the_url_it_is_given(fake_stealth):
    visited = fake_stealth("<html>ok</html>", [{"name": "cf_clearance", "value": "x"}])
    run_stealth("https://host-a.example/f/abc")
    assert visited == ["https://host-a.example/f/abc"]


def test_browser_proxy_hands_chromium_an_ip(monkeypatch):
    monkeypatch.setattr(transport, "proxy", None)
    assert transport.browser_proxy() is None

    monkeypatch.setattr(transport, "proxy", "socks5://px.web.1:1080")
    monkeypatch.setattr(transport.socket, "gethostbyname", lambda h: "10.0.0.7")
    assert transport.browser_proxy() == "socks5://10.0.0.7:1080"

    monkeypatch.setattr(transport, "proxy", "http://u:p@px.web.1:8080")
    assert transport.browser_proxy() == "http://u:p@10.0.0.7:8080"

    for untouched in ["socks5://px.example:1080", "http://[::1]:8080", "px.web.1:1080"]:
        monkeypatch.setattr(transport, "proxy", untouched)
        assert transport.browser_proxy() == untouched


def test_idle_cap_reaches_scraplings_own_wait():
    """The override shadows a private scrapling method; an upgrade that renames it
    would silently bring the full-timeout wait back."""
    from scrapling.fetchers import StealthySession

    assert hasattr(StealthySession, "_wait_for_networkidle")
    waits = []
    page = SimpleNamespace(wait_for_load_state=lambda state, timeout: waits.append(timeout))
    for asked in (None, 30000, 2000):
        transport.IdleCappedSession._wait_for_networkidle(page, asked)
    assert waits == [5000, 5000, 2000]


def aged_jar(store, age):
    d = store.get("walled.example")
    d.cookies, d.headers = {"cf_clearance": "old"}, {"user-agent": "ua-old"}
    d.solved_at = time.monotonic() - age
    return d


def test_an_aging_jar_is_renewed_while_the_old_one_keeps_serving(monkeypatch):
    store = SessionStore()
    d = aged_jar(store, transport.RENEW_AFTER_S + 1)
    release = threading.Event()

    def solve(url):
        assert d.cookies == {"cf_clearance": "old"}, "callers keep the old jar meanwhile"
        release.wait(2)
        return {"cf_clearance": "new"}, {"user-agent": "ua-new"}

    monkeypatch.setattr(transport, "run_stealth", solve)
    thread = transport.renew_if_aging(store, "https://walled.example/api?q=1")
    assert transport.renew_if_aging(store, "https://walled.example/x") is None, "one at a time"
    release.set()
    thread.join(2)
    assert (d.cookies, d.headers) == ({"cf_clearance": "new"}, {"user-agent": "ua-new"})
    assert not d.stealth_lock.locked()


def test_a_fresh_jar_is_left_alone(monkeypatch):
    store = SessionStore()
    aged_jar(store, 5)
    monkeypatch.setattr(transport, "run_stealth", lambda url: pytest.fail("no renewal"))
    assert transport.renew_if_aging(store, "https://walled.example/") is None


def test_a_renewal_without_clearance_keeps_the_old_jar(monkeypatch):
    store = SessionStore()
    d = aged_jar(store, transport.RENEW_AFTER_S + 1)
    monkeypatch.setattr(transport, "run_stealth", lambda url: ({"other": "x"}, {}))
    transport.renew_if_aging(store, "https://walled.example/").join(2)
    assert d.cookies == {"cf_clearance": "old"}
    assert not d.stealth_lock.locked()
