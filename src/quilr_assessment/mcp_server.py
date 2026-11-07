import asyncio
import json
import logging
import sys
from io import TextIOWrapper
from typing import Annotated

import anyio
from mcp import McpError, types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .stdio import LockedOutput, ValidatedInput

CustomerId = Annotated[str, Field(strict=True, pattern=r"^CUST-[0-9]{5}$")]


class CustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: CustomerId


class RefundInput(CustomerInput):
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=10, max_length=2000, pattern=r"\S")]


server = Server("customer-tools")
SCHEMAS = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_customer_record",
            description="Read a synthetic customer record. No real customer data is used.",
            inputSchema=CustomerInput.model_json_schema(),
        ),
        types.Tool(
            name="trigger_refund",
            description="Validate and simulate a refund. Does not move money.",
            inputSchema=RefundInput.model_json_schema(),
        ),
    ]


async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
    schema = SCHEMAS.get(request.params.name)
    if schema is None:
        raise McpError(types.ErrorData(code=-32602, message="Unknown tool"))
    try:
        arguments = schema.model_validate(request.params.arguments or {})
    except ValidationError as exc:
        # Field locations are useful; raw input values may contain sensitive data.
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
        raise McpError(
            types.ErrorData(code=-32602, message="Invalid tool arguments", data={"fields": fields})
        ) from None

    if isinstance(arguments, RefundInput):
        result = {
            "customer_id": arguments.customer_id,
            "amount": arguments.amount,
            "status": "simulated",
        }
    else:
        result = {"customer_id": arguments.customer_id, "name": "Demo Customer", "status": "active"}
    return types.ServerResult(
        types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))])
    )


# The SDK's call_tool decorator turns exceptions into tool-result errors. The
# assessment requires a JSON-RPC -32602 response, so register at the request layer.
server.request_handlers[types.CallToolRequest] = call_tool


async def run() -> None:
    output = LockedOutput(anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8")))
    input_file = anyio.wrap_file(sys.stdin.buffer)
    async with stdio_server(stdin=ValidatedInput(input_file, output), stdout=output) as (
        read,
        write,
    ):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(run())


if __name__ == "__main__":
    main()
