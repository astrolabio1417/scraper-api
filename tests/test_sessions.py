import time

import sessions
from sessions import SessionStore, host, pick_cookies, status_key


def test_status_key_groups_sibling_hosts():
    a = status_key("https://cdn-05.host-c.example/mp4/x")
    b = status_key("https://cdn-06.host-c.example/mp4/y")
    assert a == b == "host-c.example/mp4"
    # Verdicts are shared, credentials are not.
    assert host("https://cdn-05.host-c.example/x") != host("https://cdn-06.host-c.example/y")
    assert status_key("https://host-a.example/f/abc") == "host-a.example/f"


def test_status_key_separates_paths_on_one_host():
    assert status_key("https://host-a.example/f/abc") != status_key("https://host-a.example/e/abc")
    assert status_key("https://host-a.example/") == "host-a.example"


def test_status_key_does_not_group_ip_literals():
    assert status_key("https://192.168.1.10/x") == "192.168.1.10/x"
    assert status_key("https://10.0.0.1:8080/y") == "10.0.0.1/y"
    assert status_key("https://192.168.1.10/x") != status_key("https://10.0.1.10/x")
    assert status_key("https://localhost/a") == "localhost/a"


def test_pick_cookies():
    jar = {"app_session": "s", "cf_clearance": "c"}
    assert pick_cookies(jar, "cf") == {"cf_clearance": "c"}
    assert pick_cookies(jar, "all") == jar
    assert pick_cookies(jar, "all") is not jar


def test_cooldown_expires(monkeypatch):
    monkeypatch.setattr(sessions, "STEALTH_COOLDOWN_S", 0)
    store = SessionStore()
    store.get("expired.example").stealth_failed_at = time.monotonic()
    assert not store.in_cooldown("expired.example")


def test_set_status_true_clears_cooldown():
    store = SessionStore()
    store.get("k").stealth_failed_at = time.monotonic()
    store.set_status("k", True)
    assert not store.in_cooldown("k")


def test_snapshot_only_lists_domains_with_a_jar():
    store = SessionStore()
    store.get("empty.example")
    store.get("full.example").cookies = {"cf_clearance": "x"}
    assert list(store.snapshot()) == ["full.example"]
