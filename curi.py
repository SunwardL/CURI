#!/usr/bin/env python3
"""CURI: local Codex usage and relay reliability dashboard.

The runtime uses only Python's standard library. It reads local JSONL files and
serves a loopback-only dashboard; prompts, responses, credentials and API keys
are never persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
  lines INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns(
  source_file TEXT NOT NULL, turn_id TEXT NOT NULL, ts TEXT,
  session_id TEXT, project TEXT, requested_model TEXT, reported_model TEXT,
  input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER,
  reasoning_tokens INTEGER, duration_ms INTEGER, ttft_ms INTEGER,
  error_class TEXT, error_message TEXT,
  PRIMARY KEY(source_file, turn_id)
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
CREATE TABLE IF NOT EXISTS tools(
  event_key TEXT PRIMARY KEY, source_file TEXT NOT NULL, ts TEXT,
  name TEXT NOT NULL, category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota(
  event_key TEXT PRIMARY KEY, ts TEXT NOT NULL, raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_events(
  event_key TEXT PRIMARY KEY, ts TEXT, request_id TEXT,
  requested_model TEXT, reported_model TEXT, status INTEGER,
  error_class TEXT, attempts INTEGER, first_byte_ms INTEGER,
  duration_ms INTEGER, stream_terminal TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def classify_error(message: Any, info: Any = None) -> str | None:
    if not message and not info:
        return None
    text = as_text(message or info).lower()
    if "capacity" in text or "overloaded" in text:
        return "capacity"
    if "usage limit" in text or "limit reached" in text:
        return "usage_limit"
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    return "error"


def local_day(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        value = ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.date().isoformat()
        return parsed.astimezone().date().isoformat()
    except ValueError:
        return ts[:10] or "unknown"


def tool_category(name: str) -> str:
    value = name.lower()
    if "mcp" in value:
        return "MCP"
    if "browser" in value or "search" in value or "web" in value:
        return "Browser / search"
    if "shell" in value or "terminal" in value or "exec" in value or "command" in value:
        return "Shell"
    return "Other"


def tool_from_event(obj: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str] | None:
    event_type = as_text(obj.get("type") or payload.get("type")).lower()
    if any(word in event_type for word in ("result", "output", "return", "complete")):
        return None
    if not any(word in event_type for word in ("tool_call", "function_call", "custom_tool", "mcp_call")):
        return None
    function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    name = payload.get("name") or payload.get("tool_name") or function.get("name")
    if not name:
        name = payload.get("tool", {}).get("name") if isinstance(payload.get("tool"), dict) else "unknown"
    name = as_text(name)[:120] or "unknown"
    return name, tool_category(name)


class Store:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.last_scan = None
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _reset_file(self, conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM turns WHERE source_file=?", (path,))
        conn.execute("DELETE FROM tools WHERE source_file=?", (path,))

    def _parse_line(self, conn: sqlite3.Connection, raw: str, source: str, stats: Counter[str]) -> None:
        try:
            obj = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            stats["invalid_lines"] += 1
            return
        if not isinstance(obj, dict):
            return
        timestamp = as_text(obj.get("timestamp")) or None
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        event_type = as_text(obj.get("type"))
        payload_type = as_text(payload.get("type"))
        turn_id = as_text(payload.get("turn_id") or obj.get("turn_id"))
        if event_type == "turn_context" or payload_type == "turn_context":
            if not turn_id:
                return
            settings = payload.get("collaboration_mode", {}).get("settings", {})
            requested = as_text(settings.get("model") or payload.get("requested_model"))
            reported = as_text(payload.get("model") or payload.get("reported_model"))
            cwd = as_text(payload.get("cwd"))
            project = Path(cwd.rstrip("/\\")).name if cwd else ""
            session = Path(source).stem
            conn.execute("""INSERT INTO turns(source_file,turn_id,ts,session_id,project,requested_model,reported_model)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_file,turn_id) DO UPDATE SET
                ts=COALESCE(excluded.ts,turns.ts), session_id=COALESCE(excluded.session_id,turns.session_id),
                project=CASE WHEN excluded.project!='' THEN excluded.project ELSE turns.project END,
                requested_model=CASE WHEN excluded.requested_model!='' THEN excluded.requested_model ELSE turns.requested_model END,
                reported_model=CASE WHEN excluded.reported_model!='' THEN excluded.reported_model ELSE turns.reported_model END""",
                (source, turn_id, timestamp, session, project, requested, reported))
            stats["turns"] += 1
        elif event_type == "token_usage_record":
            usage = payload.get("turn_token_usage") or payload.get("usage") or {}
            if turn_id and isinstance(usage, dict):
                conn.execute("""UPDATE turns SET input_tokens=?,cached_tokens=?,output_tokens=?,reasoning_tokens=?
                    WHERE source_file=? AND turn_id=?""", (
                    as_int(usage.get("input_tokens")), as_int(usage.get("cached_input_tokens")),
                    as_int(usage.get("output_tokens")), as_int(usage.get("reasoning_tokens")), source, turn_id))
        elif payload_type == "task_complete":
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else as_text(error)
            kind = classify_error(message, error.get("codex_error_info") if isinstance(error, dict) else None)
            conn.execute("""UPDATE turns SET duration_ms=?,ttft_ms=?,error_class=COALESCE(?,error_class),error_message=?
                WHERE source_file=? AND turn_id=?""", (as_int(payload.get("duration_ms")),
                as_int(payload.get("time_to_first_token_ms")), kind, as_text(message)[:300] or None, source, turn_id))
            if kind:
                stats["errors"] += 1
        elif payload_type == "token_count":
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                key = hashlib.sha256((source + (timestamp or "") + json.dumps(rate_limits, sort_keys=True)).encode()).hexdigest()
                conn.execute("INSERT OR IGNORE INTO quota(event_key,ts,raw) VALUES(?,?,?)",
                             (key, timestamp or now_iso(), json.dumps(rate_limits, ensure_ascii=False)))
        tool = tool_from_event(obj, payload)
        if tool:
            name, category = tool
            raw_key = source + "|" + (timestamp or "") + "|" + event_type + "|" + name + "|" + turn_id
            key = hashlib.sha256(raw_key.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO tools(event_key,source_file,ts,name,category) VALUES(?,?,?,?,?)",
                         (key, source, timestamp, name, category))

    def _scan_file(self, conn: sqlite3.Connection, path: Path, stats: Counter[str]) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        source = str(path.resolve())
        row = conn.execute("SELECT offset,size,mtime,lines FROM files WHERE path=?", (source,)).fetchone()
        unchanged = row and stat.st_size == row["offset"] and stat.st_mtime == row["mtime"]
        if unchanged:
            return
        resume = bool(row and stat.st_size >= row["offset"])
        start = int(row["offset"]) if resume else 0
        if not resume:
            self._reset_file(conn, source)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            return
        complete = data.splitlines(keepends=True)
        if complete and not complete[-1].endswith((b"\n", b"\r")):
            data = b"".join(complete[:-1])
            consumed = len(data)
            lines = complete[:-1]
        else:
            consumed = len(data)
            lines = complete
        for line in data.decode("utf-8", errors="replace").splitlines():
            self._parse_line(conn, line, source, stats)
        old_lines = int(row["lines"]) if (row and resume) else 0
        conn.execute("""INSERT INTO files(path,offset,size,mtime,lines) VALUES(?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,size=excluded.size,mtime=excluded.mtime,lines=excluded.lines""",
            (source, start + consumed, stat.st_size, stat.st_mtime, old_lines + len(lines)))
        stats["files"] += 1

    def _scan_relay(self, conn: sqlite3.Connection, path: Path, stats: Counter[str]) -> None:
        if not path.is_file():
            return
        source = str(path.resolve())
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    stats["invalid_lines"] += 1
                    continue
                if not isinstance(item, dict):
                    continue
                key = hashlib.sha256(line.encode()).hexdigest()
                conn.execute("""INSERT OR IGNORE INTO relay_events(event_key,ts,request_id,requested_model,reported_model,status,
                    error_class,attempts,first_byte_ms,duration_ms,stream_terminal) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    key, as_text(item.get("timestamp")) or None, as_text(item.get("request_id")),
                    as_text(item.get("requested_model")), as_text(item.get("reported_model")), as_int(item.get("status")),
                    as_text(item.get("error_class")) or None, as_int(item.get("attempts")), as_int(item.get("first_byte_ms")),
                    as_int(item.get("duration_ms")), as_text(item.get("stream_terminal")) or None))
                stats["relay_events"] += 1
        except OSError:
            return

    def scan(self, codex_home: str, relay_events: str = "", archive_dir: str = "") -> dict[str, Any]:
        stats: Counter[str] = Counter()
        codex = Path(codex_home).expanduser()
        files = list((codex / "sessions").rglob("*.jsonl")) if (codex / "sessions").is_dir() else []
        if archive_dir:
            archive = Path(archive_dir).expanduser()
            if archive.is_dir():
                files += list(archive.rglob("*.jsonl"))
        with self.lock, closing(self.connect()) as conn:
            for path in sorted(set(files)):
                self._scan_file(conn, path, stats)
            if relay_events:
                self._scan_relay(conn, Path(relay_events).expanduser(), stats)
            conn.commit()
        self.last_scan = now_iso()
        stats["scanned_at"] = self.last_scan
        return dict(stats)

    def summary(self, days: int = 0) -> dict[str, Any]:
        with self.lock, closing(self.connect()) as conn:
            turns = [dict(row) for row in conn.execute("SELECT * FROM turns ORDER BY ts").fetchall()]
            tools = [dict(row) for row in conn.execute("SELECT * FROM tools ORDER BY ts DESC").fetchall()]
            relay = [dict(row) for row in conn.execute("SELECT * FROM relay_events ORDER BY ts DESC LIMIT 100").fetchall()]
            quota_row = conn.execute("SELECT ts,raw FROM quota ORDER BY ts DESC LIMIT 1").fetchone()
            file_rows = [dict(row) for row in conn.execute("SELECT path,lines FROM files ORDER BY path").fetchall()]
        if days > 0:
            cutoff = time.time() - days * 86400
            turns = [row for row in turns if _timestamp_epoch(row.get("ts")) >= cutoff]
        today = datetime.now().astimezone().date().isoformat()
        today_rows = [row for row in turns if local_day(row.get("ts")) == today]
        def sums(rows: list[dict[str, Any]]) -> dict[str, Any]:
            return {"turns": len(rows), "input_tokens": sum((row.get("input_tokens") or 0) for row in rows),
                    "cached_tokens": sum((row.get("cached_tokens") or 0) for row in rows),
                    "output_tokens": sum((row.get("output_tokens") or 0) for row in rows),
                    "reasoning_tokens": sum((row.get("reasoning_tokens") or 0) for row in rows),
                    "model_calls": sum(bool(row.get("requested_model") or row.get("reported_model")) for row in rows),
                    "errors": sum(bool(row.get("error_class")) for row in rows),
                    "capacity_errors": sum(row.get("error_class") == "capacity" for row in rows),
                    "avg_duration_ms": round(sum((row.get("duration_ms") or 0) for row in rows) / len(rows)) if rows else None}
        daily: dict[str, dict[str, Any]] = {}
        for row in turns:
            bucket = daily.setdefault(local_day(row.get("ts")), {"date": local_day(row.get("ts")), "turns": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "errors": 0, "tools": 0})
            bucket["turns"] += 1
            bucket["input_tokens"] += row.get("input_tokens") or 0
            bucket["cached_tokens"] += row.get("cached_tokens") or 0
            bucket["output_tokens"] += row.get("output_tokens") or 0
            bucket["errors"] += bool(row.get("error_class"))
        tool_counts = Counter(row["category"] for row in tools)
        model_counts = Counter((row.get("reported_model") or row.get("requested_model") or "unknown") for row in turns)
        relay_retries = sum(max((row.get("attempts") or 1) - 1, 0) for row in relay)
        relay_success = sum(200 <= (row.get("status") or 0) < 400 for row in relay)
        diffs = [row for row in relay if row.get("reported_model") and row.get("requested_model") and row["reported_model"] != row["requested_model"]]
        raw_quota = json.loads(quota_row["raw"]) if quota_row else None
        return {"meta": {"generated_at": now_iso(), "last_scan": self.last_scan, "files": file_rows},
                "coverage": {"first": turns[0].get("ts") if turns else None, "last": turns[-1].get("ts") if turns else None, "turns_total": len(turns)},
                "today": sums(today_rows), "summary": sums(turns), "daily": sorted(daily.values(), key=lambda row: row["date"]),
                "models": [{"model": name, "turns": count} for name, count in model_counts.most_common()],
                "tools": {"total": len(tools), "by_category": dict(tool_counts), "recent": tools[:20]},
                "quota": {"timestamp": quota_row["ts"] if quota_row else None, "windows": raw_quota},
                "relay": {"total": len(relay), "success": relay_success, "retries": relay_retries, "model_differences": len(diffs), "recent": relay[:20]}}


def _timestamp_epoch(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def doctor(codex_home: str, relay_events: str, db_path: str, archive_dir: str = "") -> tuple[bool, list[dict[str, Any]]]:
    checks = []
    codex = Path(codex_home).expanduser()
    sessions = codex / "sessions"
    checks.append({"name": "Codex home", "path": str(codex), "ok": codex.is_dir()})
    checks.append({"name": "Sessions directory", "path": str(sessions), "ok": sessions.is_dir()})
    if archive_dir:
        archive = Path(archive_dir).expanduser()
        checks.append({"name": "Archive directory", "path": str(archive), "ok": archive.is_dir()})
    relay = Path(relay_events).expanduser()
    checks.append({"name": "Relay events", "path": str(relay), "ok": relay.is_file(), "optional": True})
    db = Path(db_path).expanduser()
    checks.append({"name": "Database parent", "path": str(db.parent), "ok": db.parent.is_dir()})
    return all(item["ok"] or item.get("optional") for item in checks), checks


HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CURI</title>
<style>
:root{--bg:#0c1117;--panel:#121a24;--line:#243243;--ink:#e9f1f7;--muted:#8da0b5;--cyan:#57e3d0;--orange:#ffb45b;--red:#ff7388}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% -20%,#1b3540 0,transparent 40%),var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:42px 24px 72px}.eyebrow{color:var(--cyan);font:700 12px/1.2 ui-monospace,monospace;letter-spacing:.16em;text-transform:uppercase}.hero{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:34px}.hero h1{font:800 clamp(36px,6vw,70px)/.95 Georgia,serif;letter-spacing:-.06em;margin:10px 0}.hero p{color:var(--muted);max-width:580px;margin:0}.pulse{border:1px solid #31525b;border-radius:999px;padding:8px 12px;color:var(--cyan);font:12px ui-monospace,monospace;white-space:nowrap}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:linear-gradient(145deg,#16222d,#101720);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 20px 60px #0003}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em}.value{font:700 29px/1.1 Georgia,serif;margin:9px 0}.small{color:var(--muted);font-size:12px}.section{margin-top:24px}.section h2{font:700 19px Georgia,serif;margin:0 0 12px}.wide{grid-column:span 2}.chart{height:210px;display:flex;align-items:end;gap:7px;padding-top:16px}.bar{background:linear-gradient(180deg,var(--cyan),#298a94);border-radius:5px 5px 2px 2px;min-width:10px;flex:1;position:relative}.bar span{position:absolute;top:100%;font-size:10px;color:var(--muted);transform:translateX(-20%);margin-top:7px}.rows{display:grid;gap:8px}.row{display:flex;justify-content:space-between;gap:14px;border-bottom:1px solid #ffffff0a;padding:7px 0}.tag{font:12px ui-monospace,monospace;color:var(--orange)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid #ffffff10}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.1em}.ok{color:var(--cyan)}.bad{color:var(--red)}@media(max-width:820px){.grid{grid-template-columns:repeat(2,1fr)}.wide{grid-column:span 2}.hero{display:block}.pulse{display:inline-block;margin-top:14px}}@media(max-width:520px){main{padding:28px 14px}.grid{grid-template-columns:1fr}.wide{grid-column:span 1}}
</style></head><body><main><div class="hero"><div><div class="eyebrow">CURI / local observability</div><h1>Know the request<br>behind the request.</h1><p>A private, loopback-only view of Codex usage, tools, quotas and relay behavior. No prompts. No responses. No telemetry.</p></div><div class="pulse" id="updated">waiting for first scan</div></div><div class="grid" id="cards"></div><div class="section grid"><div class="card wide"><h2>Daily signal</h2><div class="chart" id="chart"></div></div><div class="card"><h2>Quota windows</h2><div class="rows" id="quota"></div></div><div class="card"><h2>Tool activity</h2><div class="rows" id="tools"></div></div></div><div class="section grid"><div class="card wide"><h2>Models observed</h2><div class="rows" id="models"></div></div><div class="card"><h2>Coverage</h2><div class="rows" id="coverage"></div></div><div class="card wide"><h2>Recent relay events</h2><table><thead><tr><th>Time</th><th>Requested</th><th>Reported</th><th>Status</th><th>Attempts</th><th>Latency</th></tr></thead><tbody id="relay"></tbody></table></div></div></main><script>
const $=id=>document.getElementById(id), n=v=>v==null?'—':Number(v).toLocaleString(), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function card(label,value,sub,cls=''){return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="small">${sub||''}</div></div>`}
function render(d){const t=d.today||{},r=d.relay||{};$('updated').textContent=`scanned ${d.meta.last_scan||'—'}`;$('cards').innerHTML=card('Today · tokens',n((t.input_tokens||0)+(t.output_tokens||0)),`${n(t.input_tokens)} in · ${n(t.output_tokens)} out`)+card('Today · turns',n(t.turns),`${n(t.model_calls)} identifiable model calls`)+card('Relay · success',r.total?`${Math.round(r.success*100/r.total)}%`:'—',`${n(r.total)} events · ${n(r.retries)} retries`,r.total&&r.success<r.total?'bad':'ok')+card('Capacity errors',n(t.capacity_errors),`${n(t.errors)} total local errors`,t.capacity_errors?'bad':'');
const days=(d.daily||[]).slice(-14),max=Math.max(1,...days.map(x=>x.input_tokens+x.output_tokens));$('chart').innerHTML=days.length?days.map(x=>`<div class="bar" style="height:${Math.max(8,(x.input_tokens+x.output_tokens)*100/max)}%"><span>${esc(x.date.slice(5))}</span></div>`).join(''):'<div class="small">No session data yet.</div>';
const windows=d.quota.windows||{};$('quota').innerHTML=d.quota.timestamp?Object.entries(windows).filter(([,v])=>v&&typeof v==='object'&&v.used_percent!=null).map(([k,v])=>`<div class="row"><span>${esc(v.name||k)}</span><strong>${esc(v.used_percent)}%</strong></div>`).join('')+`<div class="small">snapshot ${esc(d.quota.timestamp)}</div>`:'<div class="small">No rate limit snapshot available.</div>';
const tools=d.tools.by_category||{};$('tools').innerHTML=Object.entries(tools).map(([k,v])=>`<div class="row"><span>${esc(k)}</span><strong>${n(v)}</strong></div>`).join('')||'<div class="small">No tool calls observed.</div>';$('models').innerHTML=(d.models||[]).map(x=>`<div class="row"><span>${esc(x.model)}</span><span class="tag">${n(x.turns)} turns</span></div>`).join('')||'<div class="small">No model calls observed.</div>';
const cov=d.coverage||{};$('coverage').innerHTML=`<div class="row"><span>first event</span><span class="small">${esc(cov.first||'—')}</span></div><div class="row"><span>last event</span><span class="small">${esc(cov.last||'—')}</span></div><div class="row"><span>tracked turns</span><strong>${n(cov.turns_total)}</strong></div><div class="row"><span>files</span><strong>${n((d.meta.files||[]).length)}</strong></div>`;
const rows=r.recent||[];$('relay').innerHTML=rows.length?rows.map(x=>`<tr><td>${esc((x.ts||'').replace('T',' ').replace('Z',''))}</td><td>${esc(x.requested_model||'unknown')}</td><td>${esc(x.reported_model||'unknown')}</td><td class="${x.status>=200&&x.status<400?'ok':'bad'}">${esc(x.status||'—')}</td><td>${n(x.attempts||1)}</td><td>${x.duration_ms==null?'—':n(x.duration_ms)+' ms'}</td></tr>`).join(''):'<tr><td colspan="6" class="small">No relay JSONL events observed.</td></tr>'}
async function refresh(){try{const r=await fetch('/api/summary');render(await r.json())}catch(e){$('updated').textContent='waiting for CURI scanner'}}refresh();setInterval(refresh,3000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    store: Store
    config: dict[str, str]
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok", "last_scan": self.store.last_scan})
        elif self.path == "/api/summary":
            self._send(200, self.store.summary())
        elif self.path == "/":
            data = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        else:
            self.send_error(404)
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *_: Any) -> None:
        return


def serve(args: argparse.Namespace) -> None:
    store = Store(args.db)
    Handler.store = store
    def scan_loop() -> None:
        while True:
            store.scan(args.codex_home, args.relay_events, args.archive_dir)
            time.sleep(max(1, args.interval))
    threading.Thread(target=scan_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CURI listening at http://127.0.0.1:{args.port} (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CURI local Codex usage and reliability dashboard")
    sub = p.add_subparsers(dest="command", required=True)
    def common(s: argparse.ArgumentParser) -> None:
        s.add_argument("--codex-home", default=os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
        s.add_argument("--relay-events", default=os.getenv("CURI_RELAY_EVENTS", str(Path.home() / ".curi" / "relay-events.jsonl")))
        s.add_argument("--archive-dir", default=os.getenv("CURI_ARCHIVE_DIR", ""))
        s.add_argument("--db", default=os.getenv("CURI_DB", str(Path.home() / ".curi" / "curi.sqlite3")))
    s = sub.add_parser("serve", help="scan and serve the local dashboard")
    common(s); s.add_argument("--port", type=int, default=8787); s.add_argument("--interval", type=int, default=3)
    s = sub.add_parser("scan", help="scan local JSONL once and print a summary")
    common(s); s.add_argument("--days", type=int, default=0)
    s = sub.add_parser("doctor", help="check local paths without reading credentials")
    common(s); s.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "doctor":
        ok, checks = doctor(args.codex_home, args.relay_events, args.db, args.archive_dir)
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2) if args.json else "\n".join(f"{'OK' if x['ok'] else 'MISSING'}  {x['name']}: {x['path']}" for x in checks))
        return 0 if ok else 1
    store = Store(args.db)
    stats = store.scan(args.codex_home, args.relay_events, args.archive_dir)
    if args.command == "scan":
        print(json.dumps({"scan": stats, "summary": store.summary(args.days)}, ensure_ascii=False, indent=2))
        return 0
    serve(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
