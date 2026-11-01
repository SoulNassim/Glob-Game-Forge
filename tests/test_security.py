"""The local API refuses cross-site and DNS-rebinding requests."""

import httpx
import pytest

from forge.server.app import app


@pytest.fixture(autouse=True)
def _demo(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_DEMO_MODE", "1")
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path))


def client(base_url="http://127.0.0.1:8420"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def test_same_origin_and_agents_without_origin_are_allowed():
    async with client() as c:
        assert (await c.get("/api/health")).status_code == 200
        r = await c.get("/api/health", headers={"Origin": "http://127.0.0.1:8420", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200


async def test_dns_rebinding_host_is_refused():
    async with client("http://evil.example:8420") as c:
        assert (await c.get("/api/health")).status_code == 403


async def test_cross_site_post_is_refused():
    async with client() as c:
        r = await c.put("/api/config", json={"demo_mode": True}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/mcp", content="{}", headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"})
        assert r.status_code == 403


async def test_server_settings_cannot_be_changed_over_http():
    async with client() as c:
        r = await c.put("/api/config", json={"host": "0.0.0.0"})
        assert r.status_code == 403
        r = await c.put("/api/config", json={"allowed_proxy_hosts": ["internal.example"]})
        assert r.status_code == 403
