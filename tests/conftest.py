from types import SimpleNamespace

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def store():
    app_module.store.clear()
    yield app_module.store
    app_module.store.clear()


@pytest.fixture
def client():
    return app_module.app.test_client()


@pytest.fixture
def fake_stealth(monkeypatch):
    """Returns a factory: fake(html, cookies, status) -> list of visited urls."""
    import transport

    def fake(html, cookies, status=200):
        visited = []

        class Session:
            def __init__(self, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def fetch(self, url, **k):
                visited.append(url)
                return SimpleNamespace(
                    status=status,
                    body=html.encode(),
                    cookies=tuple(cookies),
                    request_headers={"user-agent": "Chrome/150.0"},
                )

        monkeypatch.setattr(transport, "StealthySession", Session)
        return visited

    return fake
