from cloudflare import CLOUDFLARE_DOM_MARKERS, is_blocked


def test_is_blocked():
    assert is_blocked(503, "nope")
    # A 200-served challenge must be caught by markup, not by the localised title.
    assert is_blocked(200, "<html><script>window._cf_chl_opt={cvId:'3'}</script>")
    assert not is_blocked(200, "<html><title>Just a Moment...</title>")
    assert not is_blocked(200, '{"ok": true}')


def test_403_escalates_only_with_challenge_markers():
    assert is_blocked(403, '<html><div id="cf-challenge-form">')
    assert not is_blocked(403, "Forbidden")


def test_jsd_challenge_under_429_escalates():
    jsd = (
        "<html><body><script>"
        "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script></body></html>"
    )
    assert is_blocked(429, jsd)


def test_the_jsd_beacon_on_a_good_page_is_not_a_challenge():
    ok = (
        "<html><body><h1>Welcome</h1>"
        "<script>a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
        "</body></html>"
    )
    assert not is_blocked(200, ok)


def test_a_plain_429_is_still_a_rate_limit():
    assert not is_blocked(429, "<html><body>429 Too Many Requests</body></html>")


def test_localised_title_is_not_a_marker():
    jp = "<html><head><title>しばらくお待ちください..."
    assert not any(m in jp.lower() for m in CLOUDFLARE_DOM_MARKERS)
