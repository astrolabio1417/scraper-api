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
def fake_camoufox(monkeypatch):
    """Returns a factory: fake(html, cookies, status) -> list of visited urls."""
    import transport

    def fake(html, cookies, status=200):
        visited = []

        class Ctx:
            def cookies(self):
                return cookies

        class Req:
            def is_navigation_request(self):
                return True

        class Page:
            context = Ctx()
            main_frame = object()

            def __init__(self):
                self._handlers = []

            def on(self, event, fn):
                self._handlers.append(fn)

            def goto(self, url, *a, **k):
                visited.append(url)
                resp = type("Resp", (), {"status": status, "request": Req()})()
                resp.frame = self.main_frame
                for fn in self._handlers:
                    fn(resp)

            def content(self):
                return html

            def evaluate(self, js):
                return "Firefox/152.0"

        class Browser:
            def __init__(self, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def new_page(self):
                return Page()

        monkeypatch.setattr(transport, "Camoufox", Browser)
        monkeypatch.setattr(transport, "CHALLENGE_WAIT_S", 0)
        return visited

    return fake
