"""The Ollama brain — local, private, no key. Uses Ollama's /api/chat with tool
calling (llama3.1 and similar support it). Falls back cleanly if Ollama is not
running. Depends only on the stdlib so a bare clone can still select it."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import Brain, ToolCall, Turn


class OllamaBrain(Brain):
    name = "ollama"

    def __init__(self, host: str, model: str):
        self.host = host.rstrip("/")
        self.model = model

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    def think(self, messages, tools, system) -> Turn:
        ollama_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["input_schema"],
        }} for t in tools]
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, *_flatten(messages)],
            "tools": ollama_tools,
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError) as e:
            return Turn(text=f"The local model did not respond ({e}).")

        msg = data.get("message", {})
        calls = [
            ToolCall(id=f"ollama-{i}", name=c["function"]["name"],
                     args=c["function"].get("arguments", {}))
            for i, c in enumerate(msg.get("tool_calls", []))
        ]
        return Turn(text=(msg.get("content") or "").strip(), tool_calls=calls, done=not calls)


def _flatten(messages):
    """Ollama wants plain string content; collapse our block-shaped tool
    results into readable text so a local model can still follow the thread."""
    out = []
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        else:
            parts = []
            for b in c:
                if b.get("type") == "tool_result":
                    parts.append(f"[tool result] {b.get('content', '')}")
                elif b.get("type") == "text":
                    parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    parts.append(f"[calling {b['name']} {json.dumps(b['input'])}]")
            out.append({"role": m["role"], "content": "\n".join(parts)})
    return out
