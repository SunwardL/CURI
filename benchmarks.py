"""Built-in CURI model probes for the classic candy and pelican tests.

The candy wording follows the public candy-v2 prompt shown by Help AIO:
https://www.helpaio.com/transit/crowdtest/runs/qedc67cc8h
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


CANDY_PROMPT = """不允许调用工具和联网

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

|        | 苹果味 | 桃子味 | 西瓜味 |
|--------|--------|--------|--------|
| 圆形   | 7      | 9      | 8      |
| 五角星形 | 7    | 6      | 4      |

作答与交付约定

1. 在最终回答的第一行只写一个纯阿拉伯整数，不带单位、标点或解释。
2. 第二行起可以写推理过程，仅供人工查看，不参与判分。
3. CLI 会将最终回答自动保存为 `/workspace/answer.txt`，无需你创建或修改任何文件。"""

# Canonical Simon Willison prompt: https://simonwillison.net/2024/Oct/25/pelicans-on-a-bicycle/
PELICAN_PROMPT = "Generate an SVG of a pelican riding a bicycle"


@dataclass(frozen=True)
class TestResult:
    kind: str
    model: str
    text: str
    svg: str | None
    latency_ms: int


def _strip_svg_fences(text: str) -> str:
    match = re.search(r"<svg\b[\s\S]*?</svg>", text, re.IGNORECASE)
    return match.group(0) if match else ""


def sanitize_svg(text: str) -> str:
    """Keep SVG display-only: remove scripts, event handlers and external URLs."""
    source = _strip_svg_fences(text)
    if not source:
        return ""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return ""
    blocked = {"script", "foreignobject", "iframe", "object", "embed"}

    def clean(parent: ET.Element) -> None:
        for child in list(parent):
            tag = child.tag.rsplit("}", 1)[-1].lower() if isinstance(child.tag, str) else ""
            if tag in blocked:
                parent.remove(child)
                continue
            for key, value in list(child.attrib.items()):
                local = key.rsplit("}", 1)[-1].lower()
                lowered = value.strip().lower()
                if local.startswith("on") or local in {"href", "src"} and not lowered.startswith("data:"):
                    del child.attrib[key]
            clean(child)

    clean(root)
    if root.tag.startswith("{http://www.w3.org/2000/svg}"):
        ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return str(content or "")
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if isinstance(content, dict) and content.get("text"):
                    parts.append(str(content["text"]))
        return "".join(parts)
    return str(payload.get("output_text") or payload.get("text") or "")


class TestRunner:
    def __init__(self, base_url: str, model: str, api_key: str = "", api_format: str = "responses", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_format = api_format
        self.timeout = timeout

    def run(self, kind: str) -> TestResult:
        if kind not in {"candy", "pelican"}:
            raise ValueError("unknown test kind")
        prompt = CANDY_PROMPT if kind == "candy" else PELICAN_PROMPT
        if self.api_format == "chat":
            path = "/chat/completions"
            payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        else:
            path = "/responses"
            payload = {"model": self.model, "input": prompt, "stream": False}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(urljoin(self.base_url + "/", path.lstrip("/")), data=json.dumps(payload).encode(), headers=headers, method="POST")
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(f"model request failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"model request failed: {exc}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("model returned invalid JSON") from exc
        text = _response_text(decoded).strip()
        svg = sanitize_svg(text) if kind == "pelican" else None
        return TestResult(kind, self.model, text, svg, round((time.monotonic() - started) * 1000))
