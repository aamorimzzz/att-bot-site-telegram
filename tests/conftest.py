import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from attsite.config import Config  # noqa: E402
from attsite.storage import Storage  # noqa: E402


class FakeSite:
    """Servidor HTTP falso: cada URL devolve o que estiver em `pages`."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str, dict]] = {}
        self.requests: list[httpx.Request] = []

    def set(self, url: str, body: str, status: int = 200, headers: dict | None = None):
        self.pages[url] = (status, body, headers or {"content-type": "text/html; charset=utf-8"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.pages:
            raise httpx.ConnectError("sem rota", request=request)
        status, body, headers = self.pages[url]
        etag = headers.get("etag")
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(status, text=body, headers=headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=True)


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def cfg(tmp_path):
    return Config(token="123456:TEST", allowed_users=frozenset(), db_path=tmp_path / "t.db",
                  log_file=tmp_path / "t.log", min_interval=1, error_alert_after=3,
                  sec_user_agent="Fulano fulano@example.com")


@pytest.fixture
def storage(cfg):
    s = Storage(cfg.db_path)
    yield s
    s.close()
