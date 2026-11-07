# Validation results

Recorded on November 6, 2026. These measurements use synthetic data and local
mock providers; they are not latency or capacity claims for a live LLM service.

## Checks completed

| Check | Result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Local Python 3.12 test suite | 34 passed in 0.75 seconds |
| Test suite in the built container, without external networking | 34 passed in 0.93 seconds |
| HTTP walkthrough against two real local server processes | Passed |
| Docker image build from the hash-locked requirements | Passed |
| Docker Compose HTTP request, redaction, and persistent SQLite volume | Passed as default UID 10001 |

The container test uses a non-root UID matching the read-only mounted review
directory. A separate Compose run verifies the image's default UID 10001 and its
writable SQLite volume. The suite includes actual
stdio process I/O, rather than only calling tool functions directly.

The tests exercise every split point for representative PII strings, 100 random
chunkings of combined PII, byte-by-byte UTF-8/SSE framing, forged/expired JWTs,
malformed JSON, notification handling, tenant isolation, concurrent database
admission, restart persistence, both fallback conditions, cancellation, and
failure sanitization. An unfinished PII candidate is not flushed when a provider
stream fails.

## Example container walkthrough

```text
MCP discovery: get_status, admin_reset_key
Viewer admin call: -32001 Unauthorized Tool Call
Admin admin call: allowed
Stream: Hello! Email [REDACTED], SSN [REDACTED], card [REDACTED]. Done.
Local mock TTFT: 20.3 ms; complete: 159.4 ms
demo:429: secondary selected in 10.6 ms
demo:timeout: secondary selected in 3010.6 ms
Provider failure: 502 upstream_error, with a request ID and no upstream body
Quota exceeded: 429 rate_limit_exceeded
```

The mock emits three-character deltas with a five-millisecond pause. TTFT is
measured at the client when the first nonempty redacted text delta arrives.
Timeout fallback includes the primary's three-second deadline plus the local
secondary response. Other local runs observed TTFT around 20–33 ms.

## Small load check

`python scripts/benchmark.py` processed 1,100,000 input bytes in 37-character
chunks. One run took 0.785 seconds, approximately 1.34 MiB/s. The largest pending
candidate in that workload was 19 characters; a separate adversarial test checks
the hard 512-character limit using a 100,000-character candidate.

For 100 simultaneous reservations of 1,000 tokens against a 50,000-token budget,
exactly 50 succeeded and 50 were rejected. The observed SQLite reservation
latencies were 58.07 ms at p50 and 66.36 ms at p95 on this shared environment.
These include thread scheduling and SQLite lock contention. No production
throughput target is inferred from this small run.

## Remaining integration work

Live provider calls were not exercised: no real provider credentials were
supplied. The generic adapter is tested with controlled HTTP responses and a
running local provider. Before deployment, validate the selected providers'
payload, streaming usage, token-framing assumptions, and authentication settings.

GitHub Actions has been configured but has not run remotely before the initial
push. The exact checks in the workflow were run locally. See the design notes
for the limited PII pattern coverage and single-host SQLite deployment boundary.
