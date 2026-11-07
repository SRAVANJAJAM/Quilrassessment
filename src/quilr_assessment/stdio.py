"""Validate the JSON-RPC envelope before handing messages to the official SDK.

The SDK logs malformed lines and maps unknown methods to invalid params. This
adapter supplies explicit protocol errors while leaving MCP sessions to the SDK.
"""

import json
from typing import get_args

import anyio
from mcp import types
from pydantic import ValidationError

METHODS = {
    request.model_fields["method"].default
    for request in get_args(types.ClientRequest.model_fields["root"].annotation)
}


def reject_constant(value: str):
    raise ValueError("Non-finite numbers are not JSON")


class LockedOutput:
    def __init__(self, output):
        self.output = output
        self.lock = anyio.Lock()

    async def write(self, text: str):
        async with self.lock:
            await self.output.write(text)
            await self.output.flush()

    async def flush(self):
        pass  # Each complete JSON-RPC line is flushed under the same lock.


class ValidatedInput:
    def __init__(self, input_file, output: LockedOutput):
        self.input = input_file
        self.output = output

    async def error(self, code: int, message: str, request_id=None):
        await self.output.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )
            + "\n"
        )

    def __aiter__(self):
        return self.lines()

    async def lines(self):
        while line := await anyio.to_thread.run_sync(self.input.wrapped.readline, 65_537):
            if len(line) > 65_536:
                while not line.endswith(b"\n"):
                    line = await anyio.to_thread.run_sync(self.input.wrapped.readline, 65_537)
                    if not line:
                        break
                await self.error(-32600, "Request exceeds 64 KiB")
                continue
            try:
                raw = json.loads(line, parse_constant=reject_constant)
            except ValueError:
                await self.error(-32700, "Parse error")
                continue
            try:
                types.JSONRPCMessage.model_validate(raw, strict=True)
            except ValidationError:
                await self.error(-32600, "Invalid Request")
                continue
            if "method" in raw and "id" in raw and raw["method"] not in METHODS:
                await self.error(-32601, "Method not found", raw["id"])
                continue
            if "method" in raw and "id" in raw:
                try:
                    types.ClientRequest.model_validate(raw, strict=True)
                except ValidationError:
                    await self.error(-32602, "Invalid params", raw["id"])
                    continue
            yield line
