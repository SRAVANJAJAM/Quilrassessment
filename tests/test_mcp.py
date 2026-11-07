import asyncio
import json
import os
import sys


async def rpc(process, message):
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()
    return json.loads(await asyncio.wait_for(process.stdout.readline(), 5))


async def test_real_stdio_transport_and_validation():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quilr_assessment.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        initialized = await rpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "assessment-test", "version": "1"},
                },
            },
        )
        assert "result" in initialized
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.drain()
        listed = await rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {x["name"] for x in listed["result"]["tools"]} == {
            "get_customer_record",
            "trigger_refund",
        }
        cases = [
            ("get_customer_record", {"customer_id": "CUST-12345"}, True),
            ("get_customer_record", {"customer_id": "CUST-12345\n"}, False),
            ("get_customer_record", {"customer_id": "CUST-1234"}, False),
            ("get_customer_record", {"customer_id": 12345}, False),
            ("get_customer_record", {"customer_id": "CUST-12345", "extra": True}, False),
            (
                "trigger_refund",
                {"customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate payment"},
                True,
            ),
        ]
        for amount in (0, -1, True, "12.5"):
            cases.append(
                (
                    "trigger_refund",
                    {"customer_id": "CUST-12345", "amount": amount, "reason": "Duplicate payment"},
                    False,
                )
            )
        for reason in ("short", "          "):
            cases.append(
                (
                    "trigger_refund",
                    {"customer_id": "CUST-12345", "amount": 1.5, "reason": reason},
                    False,
                )
            )
        for index, (tool, arguments, valid) in enumerate(cases, 3):
            result = await rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert result["id"] == index
            if valid:
                assert "result" in result
            else:
                assert result["error"]["code"] == -32602
        unknown = await rpc(process, {"jsonrpc": "2.0", "id": 99, "method": "not/a/method"})
        assert unknown["error"]["code"] == -32601
        for raw, code in [
            (b"\xff\n", -32700),
            (b"{bad\n", -32700),
            (b"[]\n", -32600),
            (
                b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                b'"params":{"name":"trigger_refund","arguments":{"amount":NaN}}}\n',
                -32700,
            ),
        ]:
            process.stdin.write(raw)
            await process.stdin.drain()
            error = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            assert error["error"]["code"] == code
            assert error["id"] is None
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
        remaining = await process.stdout.read()
        assert remaining == b"", "stdout contains unexpected non-protocol output"
        assert b"Processing request" in await process.stderr.read()
