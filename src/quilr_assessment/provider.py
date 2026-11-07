import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from .config import Settings
from .errors import GatewayError


async def sse_events(response: httpx.Response) -> AsyncIterator[dict]:
    """Parse SSE framing independently of TCP and UTF-8 character boundaries."""
    pending = b""
    data: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            size += len(line)
            if size > 65_536:
                raise GatewayError("invalid_upstream", "Provider stream event is too large")
            if not line:
                if data:
                    payload = b"\n".join(data)
                    if payload == b"[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        raise GatewayError(
                            "invalid_upstream", "Provider sent an invalid stream"
                        ) from None
                    if not isinstance(event, dict) or "error" in event:
                        raise GatewayError("invalid_upstream", "Provider stream failed")
                    yield event
                data, size = [], 0
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        if len(pending) + size > 65_536:
            raise GatewayError("invalid_upstream", "Provider stream event is too large")
    raise GatewayError("incomplete_stream", "Provider stream ended unexpectedly")


def content_delta(event: dict) -> str:
    try:
        choices = event.get("choices", [])
        if not choices:
            return ""
        value = choices[0].get("delta", {}).get("content", "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TypeError
        return value
    except (TypeError, AttributeError, IndexError, KeyError):
        raise GatewayError("invalid_upstream", "Provider sent an invalid delta") from None


def usage_tokens(result: dict) -> int | None:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("total_tokens")
    return value if type(value) is int and value >= 0 else None


@dataclass
class OpenStream:
    response: httpx.Response
    events: AsyncIterator[dict]
    first: dict
    provider: str


class Router:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client, self.settings = client, settings

    def endpoints(self):
        s = self.settings
        return [
            ("primary", s.primary_url, s.primary_key, s.primary_model, s.primary_timeout),
            ("secondary", s.secondary_url, s.secondary_key, s.secondary_model, s.secondary_timeout),
        ]

    async def complete(self, payload: dict) -> tuple[dict, str]:
        for name, url, key, model, timeout in self.endpoints():
            try:
                async with asyncio.timeout(timeout):
                    async with self.client.stream(
                        "POST",
                        url,
                        json={**payload, "model": model, "stream": False},
                        headers={"Authorization": f"Bearer {key}"} if key else {},
                    ) as response:
                        if response.status_code == 429:
                            if name == "primary":
                                continue
                            raise GatewayError(
                                "providers_unavailable", "Model capacity is unavailable", 503
                            )
                        if not response.is_success:
                            raise GatewayError("upstream_error", "Model request failed")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 1_048_576:
                                raise GatewayError(
                                    "invalid_upstream", "Provider response is too large"
                                )
                        try:
                            result = json.loads(body)
                            if not isinstance(result, dict):
                                raise ValueError
                            return result, name
                        except ValueError:
                            raise GatewayError(
                                "invalid_upstream", "Provider sent invalid JSON"
                            ) from None
            except (TimeoutError, httpx.TimeoutException):
                if name == "secondary":
                    raise GatewayError(
                        "providers_unavailable", "Model providers timed out", 504
                    ) from None
            except httpx.HTTPError:
                raise GatewayError("upstream_error", "Cannot reach model provider") from None
        raise GatewayError("providers_unavailable", "Model capacity is unavailable", 503)

    async def open_stream(self, payload: dict) -> OpenStream:
        for name, url, key, model, timeout in self.endpoints():
            response = None
            handed_off = False
            try:
                async with asyncio.timeout(timeout):
                    request = self.client.build_request(
                        "POST",
                        url,
                        json={
                            **payload,
                            "model": model,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        },
                        headers={"Authorization": f"Bearer {key}"} if key else {},
                    )
                    response = await self.client.send(request, stream=True)
                    if response.status_code == 429:
                        if name == "primary":
                            continue
                        raise GatewayError(
                            "providers_unavailable", "Model capacity is unavailable", 503
                        )
                    if not response.is_success:
                        raise GatewayError("upstream_error", "Model request failed")
                    events = sse_events(response)
                    async for first in events:
                        if content_delta(first):
                            handed_off = True
                            return OpenStream(response, events, first, name)
                    raise GatewayError("invalid_upstream", "Provider returned an empty stream")
            except (TimeoutError, httpx.TimeoutException):
                if name == "secondary":
                    raise GatewayError(
                        "providers_unavailable", "Model providers timed out", 504
                    ) from None
            except httpx.HTTPError:
                raise GatewayError("upstream_error", "Cannot reach model provider") from None
            finally:
                if response is not None and not handed_off:
                    await response.aclose()
        raise GatewayError("providers_unavailable", "Model capacity is unavailable", 503)
