import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks import TestRunner, sanitize_svg


class BenchmarkUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if "pelican" in request.get("input", "") or "pelican" in request.get("messages", [{}])[0].get("content", ""):
            text = "```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\"><script>alert(1)</script><circle cx=\"10\" cy=\"10\" r=\"8\" onclick=\"bad()\"/></svg>\n```"
            body = {"output": [{"content": [{"type": "output_text", "text": text}]}]}
        else:
            body = {"output_text": "21\n证明足够且少一颗不够。"}
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BenchmarkUpstream)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_candy_result(self):
        result = TestRunner(f"http://127.0.0.1:{self.server.server_port}/v1", "demo").run("candy")
        self.assertIn("21", result.text)
        self.assertIsNone(result.svg)

    def test_pelican_svg_is_sanitized(self):
        result = TestRunner(f"http://127.0.0.1:{self.server.server_port}/v1", "demo").run("pelican")
        self.assertIn("<svg", result.svg)
        self.assertNotIn("script", result.svg.lower())
        self.assertNotIn("onclick", result.svg.lower())

    def test_invalid_svg_is_not_rendered(self):
        self.assertEqual(sanitize_svg("not svg"), "")


if __name__ == "__main__":
    unittest.main()
