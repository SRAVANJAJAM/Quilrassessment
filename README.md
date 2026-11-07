# QuilrAI technical assessment

Four small Python components for the AI Field Developer assessment: a stdio MCP
server, an authenticated MCP proxy, a streaming PII filter, and an LLM router with
SQLite token accounting. The customer records and refunds are simulations.

## Run the review in a few minutes

Requires Python 3.12 or later. No model account, API key, or external database is
needed for the tests or demo.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .

pytest -q
python scripts/demo.py
```

The demo starts two processes on temporary loopback ports, creates fresh local
credentials and a temporary SQLite file, and exercises the gateway over real
HTTP. It checks discovery, viewer/admin authorization, redaction, both fallback
conditions, sanitized errors, and quota rejection. It stops both processes when
finished. Task 1 is tested separately through a real stdio subprocess.

Typical redacted output:

```text
Hello! Email [REDACTED], SSN [REDACTED], card [REDACTED]. Done.
```

## Where each task lives

| Task | Implementation | Main verification |
| --- | --- | --- |
| 1. MCP server | `src/quilr_assessment/mcp_server.py`, `stdio.py` | `tests/test_mcp.py`: initialization, discovery, valid calls, malformed arguments, JSON-RPC errors, stdout isolation |
| 2. Security proxy | `/mcp` in `app.py`, `auth.py` | `tests/test_proxy.py`: verified JWT roles, no downstream call on denial, transparent discovery, malformed requests |
| 3. Streaming guardrail | `redaction.py`, SSE parser in `provider.py` | `tests/test_redaction.py`, `test_gateway.py`: all split positions, fragmented UTF-8/SSE, bounded candidates, interrupted streams |
| 4. Rate limit and fallback | `limiter.py`, `provider.py` | `tests/test_limiter.py`, `test_gateway.py`: concurrent admissions, rolling expiry, persistence, usage settlement, 429/timeout failover |

The HTTP gateway uses FastAPI and HTTPX. The stdio server uses the official `mcp`
SDK and Pydantic. See [design decisions](docs/design.md) for the boundaries and
trade-offs, and [validation results](docs/validation.md) for measured local runs.

## Task 1: stdio MCP server

```bash
python -m quilr_assessment.mcp_server
```

Configure an MCP client to launch that command using the virtual environment's
Python executable. The tools are:

- `get_customer_record`: `{"customer_id": "CUST-12345"}`
- `trigger_refund`: `{"customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate payment"}`

`CUST-XXXXX` is interpreted as five ASCII digits. Inputs reject unknown fields,
wrong types, nonpositive/nonfinite amounts, and short or entirely blank reasons.
Integers are accepted as JSON numbers for the float amount; strings and booleans
are rejected. Argument errors use `-32602`, unknown methods `-32601`, malformed
JSON `-32700`, and invalid envelopes `-32600`. All logs go to stderr.

## Keep the HTTP services running

```bash
cp .env.example .env
# Terminal 1
make mock
# Terminal 2
make gateway
```

The sample credentials are for a local demonstration only. Configuration comes
from `.env` or environment variables with the `QUILR_` prefix. `.env` and SQLite
files are ignored by Git. Health is at `http://127.0.0.1:8000/health`.

### MCP authorization

```bash
VIEWER_TOKEN=$(python scripts/issue_token.py viewer)
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected error: `-32001`, `Unauthorized Tool Call`. Issue an `admin` token to
permit the same call. JWT signatures, issuer, audience, expiry, subject, and role
are checked. An unverified role claim never grants access. `tools/list` is passed
through unchanged, including admin tool descriptions, as the task specifies.

### Streaming completion

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer local-demo-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gateway","messages":[{"role":"user","content":"Hello"}],"max_tokens":100,"stream":true}'
```

The endpoint accepts a single text completion in an OpenAI-style request. Use
`stream: false` for an ordinary JSON response. The gateway chooses the actual
provider model; clients use the `gateway` alias.

The mock provider recognizes three prompts: `demo:429`, `demo:timeout`, and
`demo:error`. These triggers exist only in `mock_provider.py`. The gateway itself
has no test-only routing branches. `X-Gateway-Provider` shows the selected route;
`X-Request-ID` correlates responses with operational logs.

To use real text-completion providers, set `QUILR_PRIMARY_URL`,
`QUILR_SECONDARY_URL`, the corresponding `*_KEY` and `*_MODEL` values to full
OpenAI-style chat-completions endpoints. They must support `max_tokens`, text
content deltas, and `[DONE]`; streaming usage is requested with `include_usage`.
The gateway does not yet translate provider-specific payloads or tool calls.

## Token budget

The default is **50,000 tokens per rolling 60 seconds per tenant**. Operator-owned
API-key mappings identify tenants; a client cannot select a different tenant by
sending a header. Several keys can map to the same tenant and share its budget.

Admission reserves a conservative input allowance (UTF-8 bytes plus chat framing)
and `max_tokens`. Provider-reported `total_tokens` replaces that reservation on
successful completion. Missing usage, failure, or cancellation retains the
reservation until expiry. This can reject a request earlier than an exact
model-specific tokenizer would. It avoids undercounting text for supported
byte-level tokenizers and keeps startup independent of tokenizer downloads.

SQLite uses WAL, a busy timeout, and `BEGIN IMMEDIATE` for the entire prune/check/
insert transaction. Accounting survives restarts and works across processes on
the same local disk. Quotas measure logical gateway requests, not a provider's
billable retry attempts. See the design notes before adapting this to billing.

## Checks and containers

```bash
make lint
make test
make demo
make benchmark

# Optional local container setup, after creating .env:
docker compose up --build
```

The image runs as a non-root user. Compose keeps the mock on its internal network
and publishes only the gateway on loopback; SQLite lives in a named volume.
The GitHub Actions workflow runs lint, tests, and the HTTP demo. Workflow results
will only exist after this source is pushed.

For deployment, start with [the operational checklist](docs/design.md#deployment-notes).
The implementation is an assessment-sized reference with explicit limits, not
a claim to reproduce QuilrAI's production security platform.
