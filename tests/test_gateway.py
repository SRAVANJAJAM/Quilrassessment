import asyncio
import json
import time

import httpx
import pytest

from quilr_assessment.app import sse
from quilr_assessment.errors import GatewayError
from quilr_assessment.provider import Router, sse_events

HEADERS = {"Authorization": "Bearer test-key"}
REQUEST = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 100}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def completed():
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "Hello"}}], "usage": {"total_tokens": 10}}
    )


async def test_429_fallback_reconciles_usage_once(app_client, settings):
    seen = []

    async def provider(request):
        seen.append(request)
        if "primary" in str(request.url):
            return httpx.Response(429, json={"secret": "internal"})
        return completed()

    settings.token_limit = 145
    async with app_client(provider) as (client, _):
        for _ in range(2):
            response = await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
            assert response.status_code == 200
            assert response.headers["X-Gateway-Provider"] == "secondary"
            assert response.json()["usage"]["total_tokens"] == 10
        assert len(seen) == 4
        # Two settled requests use 20; the next 129-token reservation no longer fits.
        denied = await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
        assert denied.status_code == 429
        assert denied.headers["retry-after"]
        assert len(seen) == 4


async def test_timeout_cancels_primary_before_backup(settings):
    settings.primary_timeout = 0.025
    cancelled, calls = [], []

    async def provider(request):
        calls.append(str(request.url))
        if "primary" in str(request.url):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.append(True)
        assert cancelled == [True]
        return completed()

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        started = time.monotonic()
        _, selected = await Router(client, settings).complete(REQUEST)
        assert selected == "secondary"
        assert time.monotonic() - started < 0.5
        assert len(calls) == 2


@pytest.mark.parametrize("status,expected", [(500, 502), (401, 502), (429, 503)])
async def test_errors_are_sanitized(app_client, status, expected):
    def provider(request):
        return httpx.Response(status, text="SECRET STACK /private/provider token=secret")

    async with app_client(provider) as (client, _):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
        assert response.status_code == expected
        assert "SECRET" not in response.text and "private" not in response.text
        assert set(response.json()["error"]) == {"code", "message", "request_id"}
        assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


async def test_stream_redacts_deltas_reframes_and_closes(app_client):
    source = "Hi 你好! alice@example.com, 123-45-6789, 4111 1111 1111 1111."
    events = "".join(sse({"choices": [{"delta": {"content": c}}]}) for c in source)
    events += sse({"choices": [], "usage": {"total_tokens": 20}}) + sse("[DONE]")
    wire = events.replace("\n", "\r\n").encode()
    stream = Chunks([wire[i : i + 7] for i in range(0, len(wire), 7)])
    async with app_client(lambda _: httpx.Response(200, stream=stream)) as (client, _):
        response = await client.post(
            "/v1/chat/completions", headers=HEADERS, json={**REQUEST, "stream": True}
        )
        assert response.status_code == 200
        text = ""
        for line in response.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                text += json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        assert text == "Hi 你好! [REDACTED], [REDACTED], [REDACTED]."
        assert "alice" not in response.text
        assert response.text.endswith("data: [DONE]\n\n")
        assert stream.closed


async def test_parser_does_not_wait_for_whole_response():
    release = asyncio.Event()

    class Controlled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"choices": [{"delta": {"content": "Hello "}}]}).encode()
            await release.wait()
            yield b"data: [DONE]\n\n"

    response = httpx.Response(200, stream=Controlled())
    events = sse_events(response)
    first = await asyncio.wait_for(anext(events), 0.1)
    assert first["choices"][0]["delta"]["content"] == "Hello "
    release.set()
    assert [event async for event in events] == []


async def test_utf8_and_multiline_sse():
    data = (
        b':keepalive\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"'
        + "你好".encode()
        + b'"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
    )
    response = httpx.Response(200, stream=Chunks([bytes([c]) for c in data]))
    events = [event async for event in sse_events(response)]
    assert events == [{"choices": [{"delta": {"content": "你好"}}]}]


@pytest.mark.parametrize(
    "wire", [b"data: not-json\n\n", b"data: " + b"a" * 65_537, b'data: {"error":"PRIVATE"}\n\n']
)
async def test_bad_or_oversized_stream_is_rejected(wire):
    response = httpx.Response(200, stream=Chunks([wire]))
    with pytest.raises(GatewayError):
        _ = [event async for event in sse_events(response)]


async def test_broken_stream_does_not_flush_pii_or_fallback(app_client):
    calls = []
    stream = Chunks([sse({"choices": [{"delta": {"content": "Hello alice@"}}]}).encode()])

    def provider(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    async with app_client(provider) as (client, _):
        response = await client.post(
            "/v1/chat/completions", headers=HEADERS, json={**REQUEST, "stream": True}
        )
        assert "Hello " in response.text and "alice" not in response.text
        assert "event: error" in response.text
        assert "incomplete_stream" in response.text
        assert "[DONE]" not in response.text
        assert len(calls) == 1 and stream.closed


async def test_request_limits_auth_and_tenant_isolation(app_client, settings):
    settings.token_limit = 129

    # No usage in this response: keep the full reservation conservatively.
    def provider(_):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hi"}}]})

    async with app_client(provider) as (client, _):
        assert (await client.post("/v1/chat/completions", json=REQUEST)).status_code == 401
        assert (
            await client.post(
                "/v1/chat/completions", headers=HEADERS, json={**REQUEST, "max_tokens": True}
            )
        ).status_code == 422
        assert (
            await client.post("/v1/chat/completions", headers=HEADERS, content=b"x" * 65_537)
        ).status_code == 413
        assert (
            await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
        ).status_code == 200
        assert (
            await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
        ).status_code == 429
        assert (
            await client.post(
                "/v1/chat/completions", headers={"Authorization": "Bearer other-key"}, json=REQUEST
            )
        ).status_code == 200


async def test_stream_timeout_before_first_delta_falls_back_and_closes(settings):
    settings.primary_timeout = 0.02
    slow = Chunks([sse({"choices": [{"delta": {"content": "late"}}]}).encode()], delay=1)
    fast = Chunks(
        [sse({"choices": [{"delta": {"content": "Hello "}}]}).encode(), b"data: [DONE]\n\n"]
    )

    def provider(request):
        return httpx.Response(200, stream=slow if "primary" in str(request.url) else fast)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        opened = await Router(client, settings).open_stream(REQUEST)
        assert opened.provider == "secondary" and slow.closed
        assert [event async for event in opened.events] == []
        await opened.response.aclose()
        assert fast.closed


async def test_caller_cancellation_does_not_trigger_fallback(settings):
    started = asyncio.Event()
    calls = []

    async def provider(request):
        calls.append(request)
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        task = asyncio.create_task(Router(client, settings).complete(REQUEST))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1


async def test_both_provider_timeouts_are_sanitized(app_client, settings):
    settings.primary_timeout = settings.secondary_timeout = 0.01

    async def provider(request):
        await asyncio.sleep(1)
        return completed()

    async with app_client(provider) as (client, _):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "providers_unavailable"
