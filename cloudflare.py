CLOUDFLARE_STATUS_CODES = {503}
# The interstitial is served in the egress IP's language, so its title is not a marker.
CLOUDFLARE_DOM_MARKERS = [
    "_cf_chl_opt",
    'id="cf-challenge-form"',
    "cf-browser-verification",
    'id="challenge-running"',
    'id="cf-please-wait"',
]
# Also injected into healthy 200 pages for fingerprinting; a challenge only with an error status.
CLOUDFLARE_ERROR_DOM_MARKERS = [
    "/cdn-cgi/challenge-platform/",
    "__cf$cv$params",
]


def is_blocked(status_code, body):
    """A bare 403 is an auth denial, not a challenge; it escalates only with a marker."""
    lower = (body or "").lower()
    if any(m in lower for m in CLOUDFLARE_DOM_MARKERS):
        return True
    if status_code >= 400 and any(m in lower for m in CLOUDFLARE_ERROR_DOM_MARKERS):
        return True
    return status_code in CLOUDFLARE_STATUS_CODES
