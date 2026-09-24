# CURI — Codex Usage & Reliability Insights

CURI is a local, privacy-first dashboard for Codex usage and OpenAI-compatible relay reliability. It reads local JSONL files, stores only aggregate metadata in SQLite, and serves a loopback-only dashboard.

## What it shows

- latest quota windows from `token_count.rate_limits` (unknown windows stay unknown)
- today and daily history for input, cached, output and reasoning tokens
- turns, identifiable model calls, errors and capacity failures
- daily trend filtering by observed model and project
- tool calls grouped as Shell, MCP, Browser/search and Other
- structured relay events: status, attempts, latency, terminal state and requested/reported model differences
- coverage dates and the last scan time

CURI does not read `auth.json`, request bodies, prompts, response text or API keys. It sends no telemetry.

## Quick start

Python 3.10+ is enough.

```bash
python curi.py doctor
python curi.py serve
```

Open <http://127.0.0.1:8787>. CURI scans `~/.codex/sessions` every three seconds. Override paths when needed:

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

The relay side is intentionally an input contract. A relay (including Steady Relay or your own proxy) can append one JSON object per line:

```json
{"schema_version":1,"timestamp":"2026-09-25T12:00:00Z","request_id":"req-1","requested_model":"model-a","reported_model":"model-a","status":200,"attempts":2,"first_byte_ms":420,"duration_ms":3800,"error_class":null,"stream_terminal":"response.completed"}
```

Do not write secrets or full payloads to that file. `reported_model` remains `unknown` in the UI when the upstream did not report one.

## Development

```bash
python -m unittest -v
python -m py_compile curi.py
```

The project deliberately has no runtime dependencies. The dashboard is served by Python's standard library. The scanner uses file offsets and resumes safely after a restart; a truncated or rewritten JSONL file is rescanned from the beginning.

## Design boundaries

CURI observes local events; it does not automatically route between providers, run probes, evaluate answer quality, or copy Codex credentials. Relay retry safety remains the relay's responsibility: retries are only safe before real output or tool-call data has been committed to a client.

The integration direction was informed by [Steady Relay](https://github.com/937204197/steady-relay) and [Codex Model Watch](https://github.com/ysh1112/codex-model-watch). See [NOTICE.md](NOTICE.md) for attribution and license notes.

## License

MIT. See [LICENSE](LICENSE).
