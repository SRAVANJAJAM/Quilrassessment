"""Local test fixtures, deliberately separate from the gateway's production routes."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .app import sse

app = FastAPI(title="Local mock providers")
SAMPLE = "Hello! Email alice@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Done."


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mcp")
async def mcp(request: Request):
    data = await request.json()
    if "id" not in data:
        return Response(status_code=204)
    result = {"content": [{"type": "text", "text": "Mock tool executed"}]}
    if data["method"] == "tools/list":
        result = {
            "tools": [
                {"name": name, "description": "Demo tool", "inputSchema": {"type": "object"}}
                for name in ("get_status", "admin_reset_key")
            ]
        }
    return {"jsonrpc": "2.0", "id": data["id"], "result": result}


@app.post("/{provider}/v1/chat/completions")
async def completion(provider: str, request: Request):
    data = await request.json()
    prompt = data["messages"][-1]["content"]
    if provider == "primary" and prompt == "demo:429":
        return JSONResponse({"private_debug": "must not reach client"}, status_code=429)
    if provider == "primary" and prompt == "demo:timeout":
        await asyncio.sleep(3.5)
    if prompt == "demo:error":
        return JSONResponse({"stack": "SECRET INTERNAL STACK"}, status_code=500)
    usage = {"prompt_tokens": 12, "completion_tokens": 38, "total_tokens": 50}
    if not data.get("stream"):
        return {"choices": [{"message": {"role": "assistant", "content": SAMPLE}}], "usage": usage}

    async def stream():
        # Deliberately split all PII over several deltas.
        for offset in range(0, len(SAMPLE), 3):
            event = {"choices": [{"delta": {"content": SAMPLE[offset : offset + 3]}}]}
            yield sse(event)
            await asyncio.sleep(0.005)
        yield sse({"choices": [], "usage": usage})
        yield sse("[DONE]")

    return StreamingResponse(stream(), media_type="text/event-stream")
