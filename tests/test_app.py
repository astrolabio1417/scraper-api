import time

import transport


def blocked(*a, **k):
    return None, None


def test_fetch_returns_without_stealth_when_plain_works(monkeypatch, client, store):
    launches = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: launches.append(a))
    monkeypatch.setattr(transport, "fetch", lambda *a, **k: ({"success": True, "data": "hi"}, 200))

    resp = client.post("/api/fetch", json={"url": "https://clean.example/p"})

    assert resp.status_code == 200
    assert resp.get_json()["data"] == "hi"
    assert launches == []
    assert store.get("clean.example/p").plain_works is True


def test_fetch_escalates_twice_then_gives_up(monkeypatch, client, store):
    launches = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: launches.append(a))
    monkeypatch.setattr(transport, "fetch", blocked)

    resp = client.post("/api/fetch", json={"url": "https://walled.example/p"})

    assert resp.status_code == 502
    assert len(launches) == 2, "expected a root run then a target-URL run"
    assert launches[0][1] == "https://walled.example/"
    assert launches[1][1] == "https://walled.example/p"
    assert store.in_cooldown("walled.example/p")


def test_fetch_honours_cooldown(monkeypatch, client, store):
    launches = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: launches.append(k))
    monkeypatch.setattr(transport, "fetch", blocked)
    store.get("cold.example/p").stealth_failed_at = time.monotonic()

    assert client.post("/api/fetch", json={"url": "https://cold.example/p"}).status_code == 502
    assert launches == []


def test_fetch_rejects_bad_input(client):
    assert client.post("/api/fetch", json={"url": "https://x/", "method": "DELETE"}).status_code == 400
    assert client.post("/api/fetch", json={"url": "https://x/", "headers": "x"}).status_code == 400
    assert client.post("/api/fetch", json={}).status_code == 400


def test_stealth_exception_falls_through_to_cooldown(monkeypatch, client, store):
    launches = []

    def boom(*a, **k):
        launches.append(k)
        raise RuntimeError("challenge still present")

    monkeypatch.setattr(transport, "refresh_session", boom)
    monkeypatch.setattr(transport, "fetch", blocked)
    assert client.post("/api/fetch", json={"url": "https://walled.example/p"}).status_code == 502
    assert len(launches) == 2
    assert store.in_cooldown("walled.example/p")


def test_a_transport_error_does_not_disable_plain_or_cool_the_domain(monkeypatch, client, store):
    """A dead proxy is not a block: escalating on it wipes the session and buys a
    15-minute cooldown for a fault that cleared in seconds."""
    launches = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: launches.append(k))

    def dead_proxy(*a, **k):
        raise OSError("cannot complete SOCKS5 connection")

    monkeypatch.setattr(transport, "fetch", dead_proxy)

    resp = client.post("/api/fetch", json={"url": "https://flaky.example/p"})

    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]
    assert launches == []
    assert store.get("flaky.example/p").plain_works is not False
    assert not store.in_cooldown("flaky.example/p")


def test_a_challenge_url_replaces_both_stealth_passes(monkeypatch, client):
    """host-a's root answers 200 while /f/ challenges, so the root pass harvests a
    jar with no clearance in it and the caller knows better."""
    visits = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: visits.append(a))
    monkeypatch.setattr(transport, "fetch", blocked)

    client.post("/api/fetch", json={
        "url": "https://host-a.example/d/abc",
        "challenge_url": "https://host-a.example/f/abc",
    })

    assert len(visits) == 1
    assert visits[0][1] == "https://host-a.example/f/abc"


def test_the_whole_jar_is_replayed_on_request(monkeypatch, client, store):
    store.get("shop.example").cookies = {"session": "abc", "cf_clearance": "good"}
    seen = []
    monkeypatch.setattr(
        transport,
        "fetch",
        lambda url, headers, cookies, **k: (seen.append(cookies), ({"success": True}, 200))[1],
    )

    client.post("/api/fetch", json={"url": "https://shop.example/cart", "session_cookies": "all"})
    assert seen == [{"session": "abc", "cf_clearance": "good"}]


def test_caller_cookies_ride_along_with_the_clearance(monkeypatch, client, store):
    """host-a ties the download _token to the session that minted it, so the POST
    has to carry the GET's cookie without pinning everyone to one session."""
    store.get("host-a.example").cookies = {"cf_clearance": "good", "app_session": "stale"}
    seen = []
    monkeypatch.setattr(
        transport,
        "fetch",
        lambda url, headers, cookies, **k: (seen.append(cookies), ({"success": True}, 200))[1],
    )

    client.post("/api/fetch", json={
        "url": "https://host-a.example/d/abc",
        "cookies": {"app_session": "fresh"},
    })
    assert seen == [{"cf_clearance": "good", "app_session": "fresh"}]


def test_caller_cookies_are_not_mistaken_for_a_cached_session(monkeypatch, client, store):
    monkeypatch.setattr(transport, "fetch", lambda *a, **k: ({"success": True}, 200))

    client.post("/api/fetch", json={
        "url": "https://host-a.example/d/abc",
        "headers": {"Referer": "r"},
        "cookies": {"app_session": "fresh"},
    })
    assert store.get("host-a.example/d").plain_works is True


def test_cooldown_stops_repeat_browser_launches(monkeypatch, client, store):
    launches = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: launches.append(a))
    monkeypatch.setattr(transport, "stream", lambda *a: (None, "application/octet-stream"))
    store.set_status("blocked.example/dl", False)

    assert client.get("/api/download?url=https://blocked.example/dl/a").status_code == 502
    assert len(launches) == 1

    assert client.get("/api/download?url=https://blocked.example/dl/b").status_code == 502
    assert len(launches) == 1, "cooldown did not suppress the second launch"

    assert client.get("/api/download?url=https://cdn.blocked.example/dl/c").status_code == 502
    assert len(launches) == 1, "sibling host relearned the verdict with a browser"


def test_download_escalates_for_html(monkeypatch, client, store):
    ran = []
    monkeypatch.setattr(transport, "refresh_session", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(transport, "stream", lambda *a: (None, "text/html; charset=utf-8"))
    store.set_status("page.example", False)

    assert client.get("/api/download?url=https://page.example/report").status_code == 502
    assert len(ran) == 2, "expected a root run then a target-URL run"


def test_session_endpoint(client, store):
    assert client.get("/api/session").status_code == 404
    store.get("a.example").cookies = {"cf_clearance": "x"}
    assert client.get("/api/session").get_json() == {
        "a.example": {"cookies": {"cf_clearance": "x"}, "headers": {}}
    }
    assert client.get("/api/session?domain=b.example").status_code == 404
