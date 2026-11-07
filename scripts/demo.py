"""Run a complete local walkthrough, then clean up both servers."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from quilr_assessment.auth import issue_demo_token
from quilr_assessment.config import Settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def ready(client, url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Demo server failed to start; check the subprocess log")
        try:
            if (await client.get(url + "/health")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise TimeoutError("Demo server did not start")


async def walkthrough(base, client, settings):
    viewer = {"Authorization": "Bearer " + issue_demo_token("viewer", settings)}
    admin = {"Authorization": "Bearer " + issue_demo_token("admin", settings)}
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    listed = await client.post(base + "/mcp", json=rpc, headers=viewer)
    assert listed.status_code == 200
    print("MCP discovery:", ", ".join(t["name"] for t in listed.json()["result"]["tools"]))
    rpc.update(method="tools/call", params={"name": "admin_reset_key", "arguments": {}})
    denied = await client.post(base + "/mcp", json=rpc, headers=viewer)
    assert denied.json()["error"]["code"] == -32001
    print("Viewer admin call:", json.dumps(denied.json()))
    accepted = await client.post(base + "/mcp", json=rpc, headers=admin)
    assert "result" in accepted.json()
    print("Admin admin call: allowed")

    headers = {"Authorization": "Bearer " + next(iter(settings.tenant_keys))}
    request = {
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
        "stream": True,
    }
    started, first_at, safe = time.perf_counter(), None, ""
    async with client.stream(
        "POST", base + "/v1/chat/completions", headers=headers, json=request
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                part = json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                if part and first_at is None:
                    first_at = time.perf_counter()
                safe += part
    elapsed = time.perf_counter() - started
    assert safe.count("[REDACTED]") == 3 and "alice" not in safe
    assert first_at is not None and first_at - started < elapsed
    print("Stream:", safe)
    print(
        f"Local mock TTFT: {(first_at - started) * 1000:.1f} ms; complete: {elapsed * 1000:.1f} ms"
    )

    request["stream"] = False
    for trigger in ("demo:429", "demo:timeout"):
        request["messages"][0]["content"] = trigger
        started = time.perf_counter()
        response = await client.post(base + "/v1/chat/completions", headers=headers, json=request)
        assert response.status_code == 200
        assert response.headers["x-gateway-provider"] == "secondary"
        print(f"{trigger}: secondary selected in {(time.perf_counter() - started) * 1000:.1f} ms")

    request["messages"][0]["content"] = "demo:error"
    response = await client.post(base + "/v1/chat/completions", headers=headers, json=request)
    assert response.status_code == 502 and "SECRET" not in response.text
    print("Provider failure:", json.dumps(response.json()))

    request["messages"][0]["content"] = "Hello"
    request["max_tokens"] = 8192
    response = await client.post(base + "/v1/chat/completions", headers=headers, json=request)
    assert response.status_code == 429
    print("Quota exceeded:", response.status_code, response.json()["error"]["code"])


def main():
    mock_port, gateway_port = free_port(), free_port()
    mock, gateway = f"http://127.0.0.1:{mock_port}", f"http://127.0.0.1:{gateway_port}"
    with tempfile.TemporaryDirectory(prefix="quilr-demo-") as directory:
        settings = Settings(
            jwt_secret=secrets.token_urlsafe(48),
            tenant_keys={secrets.token_urlsafe(32): "demo"},
            database_path=str(Path(directory) / "usage.db"),
            token_limit=1000,
            downstream_url=mock + "/mcp",
            primary_url=mock + "/primary/v1/chat/completions",
            secondary_url=mock + "/secondary/v1/chat/completions",
            _env_file=None,
        )
        env = {
            **os.environ,
            **{
                "QUILR_" + name.upper(): json.dumps(value)
                if isinstance(value, dict)
                else str(value)
                for name, value in settings.model_dump().items()
            },
        }
        processes = []
        with open(Path(directory) / "servers.log", "w+") as logs:
            try:
                for module, port in (
                    ("quilr_assessment.mock_provider:app", mock_port),
                    ("quilr_assessment.app:create_app", gateway_port),
                ):
                    command = [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        module,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ]
                    if module.endswith("create_app"):
                        command.append("--factory")
                    processes.append(subprocess.Popen(command, env=env, stdout=logs, stderr=logs))

                async def run():
                    async with httpx.AsyncClient(timeout=10) as client:
                        await ready(client, mock, processes[0])
                        await ready(client, gateway, processes[1])
                        await walkthrough(gateway, client, settings)

                asyncio.run(run())
                print("Walkthrough passed. Both servers stopped; temporary database removed.")
            except Exception:
                logs.seek(0)
                print(logs.read(), file=sys.stderr)
                raise
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
