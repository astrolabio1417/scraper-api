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


def test_stealth_does_not_store_unsolved_challenge(fake_camoufox):
    fake_camoufox('<html><div id="cf-challenge-form">', [{"name": "cf_clearance", "value": "junk"}])
    store = SessionStore()
    with pytest.raises(RuntimeError):
        refresh_session(store, "https://walled.example/p")
    assert store.get("walled.example").cookies == {}


def test_stealth_stores_cookies_and_browser_ua(fake_camoufox):
    fake_camoufox("<html>ok</html>", [{"name": "cf_clearance", "value": "good"}])
    store = SessionStore()
    refresh_session(store, "https://walled.example/p")
    d = store.get("walled.example")
    assert (d.cookies, d.headers) == ({"cf_clearance": "good"}, {"user-agent": "Firefox/152.0"})


def test_localised_challenge_is_not_stored_as_a_solved_session(fake_camoufox):
    """Cloudflare serves the interstitial in the egress IP's language, so the
    English title never matches from a JP exit."""
    jp = (
        "<html><head><title>しばらくお待ちください..."
        '</title></head><body><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1">'
        "</script></body></html>"
    )
    fake_camoufox(jp, [{"name": "cf_clearance", "value": "junk"}], status=403)
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


def test_stealth_opens_exactly_the_url_it_is_given(fake_camoufox):
    visited = fake_camoufox("<html>ok</html>", [{"name": "cf_clearance", "value": "x"}])
    run_stealth("https://host-a.example/f/abc")
    assert visited == ["https://host-a.example/f/abc"]
