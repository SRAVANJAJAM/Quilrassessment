import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .auth import mcp_role, tenant_for
from .config import Settings
from .errors import GatewayError
from .limiter import TokenLimiter
from .provider import Router, content_delta, usage_tokens
from .redaction import StreamingRedactor, redact

log = logging.getLogger("gateway")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=32_768)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["gateway"] = "gateway"
    messages: list[Message] = Field(min_length=1, max_length=32)
    max_tokens: int = Field(default=256, ge=1, le=8192)
    stream: bool = False


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jsonrpc: Literal["2.0"]
    id: StrictInt | StrictStr | None = None
    method: str
    params: dict = Field(default_factory=dict)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: Annotated[str, Field(min_length=1)]
    arguments: dict = Field(default_factory=dict)


async def read_json(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65_536:
            raise GatewayError("request_too_large", "Request body exceeds 64 KiB", 413)
    try:
        return json.loads(body, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeDecodeError):
        raise GatewayError("invalid_json", "Invalid JSON request", 400) from None


def reject_nonfinite(value: str):
    raise ValueError("Non-finite numbers are not JSON")


def rpc_error(code: int, message: str, request_id=None, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def sse(data: dict | str) -> str:
    return f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n"


def text_chunk(request_id: str, text: str = "", *, finished: bool = False) -> dict:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "gateway",
        "choices": [
            {
                "index": 0,
                "delta": {"content": text} if text else {},
                "finish_reason": "stop" if finished else None,
            }
        ],
    }


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings()
    limiter = TokenLimiter(settings.database_path, settings.token_limit, settings.window_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await limiter.initialize()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(settings.stream_idle_timeout),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
        ) as client:
            app.state.client = client
            app.state.router = Router(client, settings)
            yield

    app = FastAPI(title="Quilr assessment gateway", version="0.1.0", lifespan=lifespan)
    app.state.limiter = limiter

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Never echo provider URLs, response bodies, credentials, or tracebacks.
            log.error("request_id=%s failure_type=%s", request.state.request_id, type(exc).__name__)
            response = JSONResponse(
                GatewayError("internal_error", "Gateway request failed", 500).payload(
                    request.state.request_id
                ),
                status_code=500,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        log.info(
            "request_id=%s method=%s status=%s headers_ms=%.2f",
            request.state.request_id,
            request.method,
            response.status_code,
            (time.monotonic() - start) * 1000,
        )
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            exc.payload(request.state.request_id), status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse(
            GatewayError("invalid_request", "Request validation failed", 422).payload(
                request.state.request_id
            ),
            status_code=422,
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_proxy(request: Request):
        try:
            role = mcp_role(request.headers.get("authorization"), settings)
        except GatewayError:
            return rpc_error(-32001, "Authentication required", status=401)
        try:
            raw = await read_json(request)
        except GatewayError as exc:
            return rpc_error(-32700 if exc.code == "invalid_json" else -32600, exc.message)
        try:
            rpc = RpcRequest.model_validate(raw)
        except ValidationError:
            return rpc_error(-32600, "Invalid Request")
        notification = "id" not in raw
        if rpc.method == "tools/call":
            try:
                call = ToolCall.model_validate(rpc.params)
            except ValidationError:
                return (
                    Response(status_code=204)
                    if notification
                    else rpc_error(-32602, "Invalid params", rpc.id)
                )
            if call.name.startswith("admin_") and role != "admin":
                return (
                    Response(status_code=204)
                    if notification
                    else rpc_error(-32001, "Unauthorized Tool Call", rpc.id)
                )
        headers = {}
        if settings.downstream_token:
            headers["Authorization"] = f"Bearer {settings.downstream_token}"
        # Only the operator controls the destination and its credentials.
        try:
            async with asyncio.timeout(3):
                async with app.state.client.stream(
                    "POST", settings.downstream_url, json=raw, headers=headers
                ) as upstream:
                    body = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            raise ValueError("Oversized MCP response")
                    if not upstream.is_success:
                        raise ValueError("Downstream failed")
                    if notification:
                        return Response(status_code=204)
                    parsed = json.loads(body)
                    if (
                        not isinstance(parsed, dict)
                        or parsed.get("jsonrpc") != "2.0"
                        or parsed.get("id") != rpc.id
                        or (("result" in parsed) == ("error" in parsed))
                    ):
                        raise ValueError("Invalid MCP response")
                    return Response(
                        bytes(body), media_type="application/json", status_code=upstream.status_code
                    )
        except (TimeoutError, httpx.HTTPError, ValueError):
            return (
                Response(status_code=204)
                if notification
                else rpc_error(-32603, "Downstream request failed", rpc.id)
            )

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        tenant = tenant_for(request.headers.get("authorization"), settings)
        raw = await read_json(request)
        try:
            body = Completion.model_validate(raw)
        except ValidationError:
            raise GatewayError("invalid_request", "Request validation failed", 422) from None
        # For byte-level tokenizers, UTF-8 bytes bound text token count from above.
        # Reserve that bound plus framing/output, then settle authoritative usage.
        # This avoids a network download or an incorrect tokenizer for the backup.
        prompt_tokens = 16 + sum(len(m.content.encode("utf-8")) + 8 for m in body.messages)
        reserved = prompt_tokens + body.max_tokens
        reservation = await limiter.reserve(tenant, reserved)
        payload = body.model_dump()
        if not body.stream:
            result, provider = await app.state.router.complete(payload)
            try:
                text = result["choices"][0]["message"]["content"]
                if not isinstance(text, str):
                    raise ValueError
            except (KeyError, IndexError, TypeError, ValueError):
                raise GatewayError(
                    "invalid_upstream", "Provider sent an invalid completion"
                ) from None
            actual = usage_tokens(result)
            if actual is not None:
                await limiter.settle(reservation, actual)
            return JSONResponse(
                {
                    "id": request.state.request_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gateway",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": redact(text)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": actual} if actual is not None else None,
                },
                headers={"X-Gateway-Provider": provider},
            )

        upstream = await app.state.router.open_stream(payload)

        async def generate():
            redactor = StreamingRedactor()
            actual = None
            try:
                async with asyncio.timeout(settings.stream_total_timeout):
                    first = redactor.feed(content_delta(upstream.first))
                    if first:
                        yield sse(text_chunk(request.state.request_id, first))
                    async for event in upstream.events:
                        value = usage_tokens(event)
                        if value is not None:
                            actual = value
                        safe = redactor.feed(content_delta(event))
                        if safe:
                            yield sse(text_chunk(request.state.request_id, safe))
                    tail = redactor.finish()
                    if tail:
                        yield sse(text_chunk(request.state.request_id, tail))
                    if actual is not None:
                        await limiter.settle(reservation, actual)
                    yield sse(text_chunk(request.state.request_id, finished=True))
                    yield sse("[DONE]")
            except (GatewayError, httpx.HTTPError, TimeoutError) as exc:
                error = (
                    exc
                    if isinstance(exc, GatewayError)
                    else GatewayError("stream_failed", "Provider stream interrupted")
                )
                yield "event: error\n" + sse(error.payload(request.state.request_id))
                # Do not flush an incomplete candidate or restart a partially sent answer.
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.response.aclose()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Gateway-Provider": upstream.provider,
            },
        )

    return app
