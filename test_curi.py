import json
import tempfile
import unittest
from pathlib import Path

from curi import HTML, Store, doctor, parser


class CuriScanTests(unittest.TestCase):
    def test_default_dashboard_port(self):
        self.assertEqual(parser().parse_args(["serve"]).port, 8792)

    def test_dashboard_has_classic_test_controls(self):
        self.assertIn("Classic tests", HTML)
        self.assertIn("/api/tests/run", HTML)

    def test_incremental_scan_counts_usage_tools_quota_and_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".codex" / "sessions" / "2026" / "09"
            sessions.mkdir(parents=True)
            rollout = sessions / "session-1.jsonl"
            relay = root / "relay-events.jsonl"
            db = root / "curi.sqlite3"
            rows = [
                {"timestamp": "2026-09-25T00:00:00Z", "type": "turn_context", "payload": {"turn_id": "t1", "model": "served-a", "cwd": "/repo/demo", "collaboration_mode": {"settings": {"model": "requested-a"}}}},
                {"timestamp": "2026-09-25T00:00:01Z", "type": "token_usage_record", "payload": {"turn_id": "t1", "turn_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_tokens": 4}}},
                {"timestamp": "2026-09-25T00:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 12.5, "resets_at": 123}}}},
                {"timestamp": "2026-09-25T00:00:03Z", "type": "mcp_tool_call", "payload": {"turn_id": "t1", "tool_name": "mcp.read"}},
                {"timestamp": "2026-09-25T00:00:04Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "duration_ms": 500, "time_to_first_token_ms": 100}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            relay.write_text(json.dumps({"schema_version": 1, "timestamp": "2026-09-25T00:00:05Z", "request_id": "r1", "requested_model": "requested-a", "reported_model": "served-a", "status": 200, "attempts": 2, "duration_ms": 900, "stream_terminal": "response.completed"}) + "\n", encoding="utf-8")
            store = Store(str(db))
            first = store.scan(str(root / ".codex"), str(relay))
            second = store.scan(str(root / ".codex"), str(relay))
            summary = store.summary()
            self.assertEqual(first["turns"], 1)
            self.assertEqual(second.get("turns", 0), 0)
            self.assertEqual(summary["summary"]["input_tokens"], 100)
            self.assertEqual(summary["summary"]["cached_tokens"], 20)
            self.assertEqual(summary["summary"]["output_tokens"], 30)
            self.assertEqual(summary["tools"]["by_category"]["MCP"], 1)
            self.assertEqual(summary["relay"]["retries"], 1)
            self.assertEqual(summary["quota"]["windows"]["primary"]["used_percent"], 12.5)

    def test_doctor_marks_optional_relay_missing_as_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions").mkdir()
            ok, checks = doctor(str(root), str(root / "missing.jsonl"), str(root / "db.sqlite3"))
            self.assertTrue(ok)
            self.assertFalse(next(item for item in checks if item["name"] == "Relay events")["ok"])


if __name__ == "__main__":
    unittest.main()
