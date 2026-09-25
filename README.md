# CURI — Codex Usage & Reliability Insights

CURI is a local, privacy-first dashboard for Codex usage and OpenAI-compatible relay reliability. It reads local JSONL files, stores only aggregate metadata in SQLite, and serves a loopback-only dashboard.

It includes the local retry relay. The relay and dashboard can run together, so CURI is both the observer and the local request boundary.

## What it shows

- latest quota windows from `token_count.rate_limits` (unknown windows stay unknown)
- today and daily history for input, cached, output and reasoning tokens
- turns, identifiable model calls, errors and capacity failures
- daily trend filtering by observed model and project
- tool calls grouped as Shell, MCP, Browser/search and Other
- structured relay events: status, attempts, latency, terminal state and requested/reported model differences
- a local OpenAI-compatible relay with transport/temporary-error retries and safe SSE reconnects
- coverage dates and the last scan time

CURI does not read `auth.json`, request bodies, prompts, response text or API keys. It sends no telemetry.

## Quick start

Python 3.10+ is enough.

```bash
python curi.py doctor
python curi.py serve --upstream https://api.example.com/v1
```

Open <http://127.0.0.1:8792>. CURI scans `~/.codex/sessions` every three seconds. Override paths when needed:

The relay listens on `http://127.0.0.1:8080/v1`; point Codex's API base URL at that address and keep the CURI process running. Your existing API key remains in Codex and is forwarded to the configured upstream; CURI never stores it.

Enable the built-in classic tests by supplying a model. The dashboard then shows two buttons under **Classic tests**:

```bash
CURI_TEST_API_KEY=your-key python curi.py serve \
  --upstream https://api.example.com/v1 \
  --test-model your-model
```

The candy button sends the fixed minimum-draw logic puzzle and displays the model's text answer. The pelican button sends `Generate an SVG of a pelican riding a bicycle` and renders a sanitized SVG result. Prompts and responses stay in memory and are never written to the usage database or relay event file; only the normal metadata-only relay event may be recorded. For a Chat Completions provider, add `--test-format chat`; for a separate compatible endpoint, use `--test-base-url`.

```bash
python curi.py serve \
  --codex-home ~/.codex \
  --relay-events ~/.curi/relay-events.jsonl \
  --archive-dir ~/.codex/archived_sessions
```

Run a one-shot scan and inspect JSON:

```bash
python curi.py scan  # use `doctor --json` for machine-readable diagnostics
```

To run only the relay:

```bash
python curi.py relay --upstream https://api.example.com/v1
```

The relay retries connection failures, timeouts, `408/425/429/5xx`, and recognized capacity/usage-limit SSE failures before real output or tool-call data reaches Codex. Once output is committed, it closes the incomplete stream instead of replaying a request that could duplicate text or a tool call. `--buffer-until-success` enables the stronger mode that holds SSE in memory until `response.completed`; its per-attempt limit is 64 MiB.

The relay appends one metadata-only JSON object per request to `~/.curi/relay-events.jsonl`:

```json
{"schema_version":1,"timestamp":"2026-09-25T12:00:00Z","request_id":"req-1","requested_model":"model-a","reported_model":"model-a","status":200,"attempts":2,"first_byte_ms":420,"duration_ms":3800,"error_class":null,"stream_terminal":"response.completed"}
```

Do not write secrets or full payloads to that file. `reported_model` remains `unknown` in the UI when the upstream did not report one.

## Development

```bash
python -m unittest -v
python -m py_compile curi.py relay.py benchmarks.py
```

The project deliberately has no runtime dependencies. The dashboard and relay use Python's standard library. The scanner uses file offsets and resumes safely after a restart; a truncated or rewritten JSONL file is rescanned from the beginning.

## Design boundaries

CURI does not automatically route between providers, run probes, evaluate answer quality, or copy Codex credentials. It keeps the relay and monitor in one project but they remain separate local roles: the relay handles forwarding/retry, while the monitor parses local usage and relay events.

The integration direction was informed by [Steady Relay](https://github.com/937204197/steady-relay) and [Codex Model Watch](https://github.com/ysh1112/codex-model-watch). See [NOTICE.md](NOTICE.md) for attribution and license notes.

## License

MIT. See [LICENSE](LICENSE).
