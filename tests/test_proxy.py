import httpx
import jwt
import pytest


async def test_viewer_blocked_before_downstream_but_discovery_transparent(app_client, tokens):
    seen = []

    def downstream(request):
        import json

        data = json.loads(request.content)
        seen.append(data)
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": data["id"],
                "result": {"tools": [{"name": "admin_reset_key"}]},
            },
        )

    async with app_client(downstream) as (client, _):
        viewer = {"Authorization": f"Bearer {tokens['viewer']}"}
        call = {
            "jsonrpc": "2.0",
            "id": "req-0",
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {}},
        }
        blocked = await client.post("/mcp", json=call, headers=viewer)
        assert blocked.json() == {
            "jsonrpc": "2.0",
            "id": "req-0",
            "error": {"code": -32001, "message": "Unauthorized Tool Call"},
        }
        assert seen == []
        listed = await client.post(
            "/mcp", json={**call, "id": 0, "method": "tools/list", "params": {}}, headers=viewer
        )
        assert listed.json()["result"]["tools"][0]["name"] == "admin_reset_key"
        assert listed.json()["id"] == 0
        await client.post("/mcp", json=call, headers={"Authorization": f"Bearer {tokens['admin']}"})
        assert len(seen) == 2
        call.pop("id")
        notification = await client.post("/mcp", json=call, headers=viewer)
        assert notification.status_code == 204 and not notification.content
        assert len(seen) == 2


async def test_auth_rejects_forged_expired_and_missing_tokens(app_client, tokens, settings):
    def never(request):
        pytest.fail("Unauthorized request reached downstream")

    payload = jwt.decode(tokens["admin"], options={"verify_signature": False})
    forged = jwt.encode(payload, "another-secret-at-least-32-bytes-long", algorithm="HS256")
    expired = jwt.encode({**payload, "exp": 1}, settings.jwt_secret, algorithm="HS256")
    async with app_client(never) as (client, _):
        for token in ("", "admin", forged, expired):
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 401


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"{bad", -32700),
        (b"[]", -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}', -32602),
    ],
)
async def test_invalid_rpc(payload, code, app_client, tokens):
    async with app_client(lambda _: pytest.fail("Invalid request forwarded")) as (client, _):
        response = await client.post(
            "/mcp", content=payload, headers={"Authorization": f"Bearer {tokens['viewer']}"}
        )
        assert response.json()["error"]["code"] == code
