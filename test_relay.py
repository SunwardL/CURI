import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from relay import RelayConfig, create_server


class FakeUpstream(BaseHTTPRequestHandler):
    attempts = 0
    mode = "retry"

    def do_POST(self):
        type(self).attempts += 1
        if self.mode == "retry" and self.attempts == 1:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"server overloaded"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"model":"served-model","ok":true}')

    def do_GET(self):
        type(self).attempts += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n")
        self.wfile.flush()
        if self.mode == "sse_retry" and self.attempts == 1:
            return
        self.wfile.write(b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"model\":\"served-model\"}\n\n")
        if self.mode == "buffer_retry" and self.attempts == 1:
            self.wfile.flush()
            return
        self.wfile.write(b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        return


def start_upstream():
    FakeUpstream.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RelayTests(unittest.TestCase):
    def test_retries_temporary_http_failure_and_writes_event(self):
        FakeUpstream.mode = "retry"
        upstream = start_upstream()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "relay.jsonl"
            relay = create_server(RelayConfig(f"http://127.0.0.1:{upstream.server_port}", port=0,
                                              max_retries=2, backoff_seconds=0, request_timeout=2,
                                              event_path=str(event_path)))
            threading.Thread(target=relay.serve_forever, daemon=True).start()
            try:
                request = Request(f"http://127.0.0.1:{relay.server_port}/v1/responses",
                                  data=b'{"model":"requested-model"}',
                                  headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"served-model", response.read())
                event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(FakeUpstream.attempts, 2)
                self.assertEqual(event["attempts"], 2)
                self.assertEqual(event["requested_model"], "requested-model")
                self.assertEqual(event["reported_model"], "served-model")
            finally:
                relay.shutdown(); relay.server_close()
        upstream.shutdown(); upstream.server_close()

    def test_retries_sse_disconnect_before_real_output(self):
        FakeUpstream.mode = "sse_retry"
        upstream = start_upstream()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "relay.jsonl"
            relay = create_server(RelayConfig(f"http://127.0.0.1:{upstream.server_port}", port=0,
                                              max_retries=2, backoff_seconds=0, request_timeout=2,
                                              event_path=str(event_path)))
            threading.Thread(target=relay.serve_forever, daemon=True).start()
            try:
                with urlopen(f"http://127.0.0.1:{relay.server_port}/v1/events", timeout=3) as response:
                    body = response.read()
                self.assertIn(b"response.output_text.delta", body)
                event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(FakeUpstream.attempts, 2)
                self.assertEqual(event["attempts"], 2)
                self.assertEqual(event["stream_terminal"], "response.completed")
            finally:
                relay.shutdown(); relay.server_close()
        upstream.shutdown(); upstream.server_close()

    def test_buffer_until_success_retries_after_output_disconnect(self):
        FakeUpstream.mode = "buffer_retry"
        upstream = start_upstream()
        with tempfile.TemporaryDirectory() as tmp:
            relay = create_server(RelayConfig(f"http://127.0.0.1:{upstream.server_port}", port=0,
                                              max_retries=2, backoff_seconds=0, request_timeout=2,
                                              event_path=str(Path(tmp) / "relay.jsonl"), buffer_until_success=True))
            threading.Thread(target=relay.serve_forever, daemon=True).start()
            try:
                with urlopen(f"http://127.0.0.1:{relay.server_port}/v1/events", timeout=3) as response:
                    self.assertIn(b"response.completed", response.read())
                self.assertEqual(FakeUpstream.attempts, 2)
            finally:
                relay.shutdown(); relay.server_close()
        upstream.shutdown(); upstream.server_close()


if __name__ == "__main__":
    unittest.main()
