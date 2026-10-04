import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

STEALTH_COOLDOWN_S = int(os.environ.get("STEALTH_COOLDOWN_S", "900"))

# Replaying an app session cookie pins every caller to one server-side session.
CF_COOKIES = ("cf_clearance", "__cf_bm")


@dataclass
class Domain:
    """Cookies/headers live on the netloc record; the verdict on the status-key record."""

    cookies: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    plain_works: bool | None = None
    stealth_failed_at: float | None = None
    stealth_lock: threading.Lock = field(default_factory=threading.Lock)


def host(url):
    return urlparse(url).netloc


def status_key(url):
    """Registrable domain plus first path segment: siblings share a verdict, paths do not."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    labels = hostname.split(".")
    if len(labels) <= 2 or hostname.replace(".", "").isdigit():
        key = hostname
    else:
        # ponytail: last two labels; co.uk-style suffixes yield a broader key, never a wrong one.
        key = ".".join(labels[-2:])
    segment = parsed.path.strip("/").split("/")[0]
    return f"{key}/{segment}" if segment else key


def pick_cookies(cookies, policy):
    if policy == "all":
        return dict(cookies)
    return {k: v for k, v in cookies.items() if k in CF_COOKIES}


class SessionStore:
    def __init__(self):
        self._domains: dict[str, Domain] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self._domains.setdefault(key, Domain())

    def clear(self):
        with self._lock:
            self._domains.clear()

    def in_cooldown(self, key):
        failed_at = self.get(key).stealth_failed_at
        return failed_at is not None and time.monotonic() - failed_at < STEALTH_COOLDOWN_S

    def set_status(self, key, plain_works):
        d = self.get(key)
        d.plain_works = plain_works
        if plain_works:
            d.stealth_failed_at = None

    def snapshot(self):
        with self._lock:
            return {
                k: {"cookies": d.cookies, "headers": d.headers}
                for k, d in self._domains.items()
                if d.cookies
            }
