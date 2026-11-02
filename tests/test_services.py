"""Signing in to a hosted MCP service (OAuth + PKCE) and calling its tools, against a fake server."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from forge import services

MCP = "https://mcp.example.test/mcp"
AUTH = "https://auth.example.test"


class FakeService:
    def __init__(self, dynamic_registration: bool = True) -> None:
        self.dynamic = dynamic_registration
        self.challenge = ""
        self.tokens_issued = 0
        self.expired = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == "https://mcp.example.test/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [AUTH]})
        if url == f"{AUTH}/.well-known/oauth-authorization-server":
            meta = {"authorization_endpoint": f"{AUTH}/authorize", "token_endpoint": f"{AUTH}/token", "scopes_supported": ["gen", "offline_access"],
                    "client_id_metadata_document_supported": True}
            if self.dynamic:
                meta["registration_endpoint"] = f"{AUTH}/register"
            return httpx.Response(200, json=meta)
        if url == f"{AUTH}/register":
            assert json.loads(request.content)["redirect_uris"] == ["http://127.0.0.1:8420/api/services/callback"]
            return httpx.Response(201, json={"client_id": "glob-123"})
        if url == f"{AUTH}/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
                assert digest == self.challenge and form["code"] == ["the-code"]
            else:
                assert form["refresh_token"] == ["r1"]
            self.tokens_issued += 1
            self.expired = False
            return httpx.Response(200, json={"access_token": f"a{self.tokens_issued}", "refresh_token": "r1", "expires_in": 3600})
        if url == MCP:
            if self.expired:
                return httpx.Response(401)
            assert request.headers["authorization"].startswith("Bearer a")
            body = json.loads(request.content)
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}}, headers={"mcp-session-id": "s1"})
            if "id" not in body:
                return httpx.Response(202)
            assert request.headers["mcp-session-id"] == "s1"
            if method == "tools/list":
                result = {"tools": [{"name": "generate_image", "description": "Make an image.", "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": f"made: {body['params']['arguments']['prompt']}"}, {"type": "resource_link", "uri": "https://cdn.example.test/1.png"}]}
            # Answer as a server-sent event, like most hosted servers.
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setitem(services.SERVICES, "demo", {"label": "Demo", "url": MCP, "makes": "tests"})
    for cache in (services._sessions, services._tools, services._pending):
        cache.clear()
    server = FakeService()
    real = httpx.Client
    monkeypatch.setattr(services.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(server.handler), **kw))
    return server


def _sign_in(fake: FakeService) -> None:
    url = services.start_sign_in("demo", "http://127.0.0.1:8420/api/services/callback")
    query = parse_qs(urlsplit(url).query)
    fake.challenge = query["code_challenge"][0]
    assert query["code_challenge_method"] == ["S256"] and query["resource"] == [MCP]
    assert "offline_access" in query["scope"][0]
    assert services.finish_sign_in(query["state"][0], "the-code") == "demo"


def test_sign_in_then_use_tools(fake: FakeService) -> None:
    _sign_in(fake)
    assert "demo" in services.connected()
    assert "generate_image: Make an image." in services.tool_summary("demo")
    text = services.call_tool("demo", "generate_image", {"prompt": "orc"})
    assert "made: orc" in text and "https://cdn.example.test/1.png" in text


def test_expired_token_is_refreshed(fake: FakeService) -> None:
    _sign_in(fake)
    services.list_tools("demo")
    fake.expired = True
    assert "made: knight" in services.call_tool("demo", "generate_image", {"prompt": "knight"})
    assert fake.tokens_issued == 2


def test_metadata_document_when_no_dynamic_registration(fake: FakeService) -> None:
    fake.dynamic = False
    url = services.start_sign_in("demo", "http://127.0.0.1:8420/api/services/callback")
    assert parse_qs(urlsplit(url).query)["client_id"] == [services.CLIENT_METADATA_URL]


def test_a_used_or_unknown_state_is_refused(fake: FakeService) -> None:
    with pytest.raises(services.ServiceError, match="expired"):
        services.finish_sign_in("nope", "the-code")
