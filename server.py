#!/usr/bin/env python3
"""Serves index.html, proxies /v1/* to LLM, manages /api/chats/* and /api/settings."""
import http.server
import urllib.request
import urllib.error
import os
import json
import re
import uuid
from datetime import datetime, timezone

PORT      = 3000
HERE      = os.path.dirname(os.path.abspath(__file__))
CHATS_DIR = os.path.join(HERE, "chats")
SETTINGS_FILE = os.path.join(HERE, "settings.json")

os.makedirs(CHATS_DIR, exist_ok=True)

CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}

# ── Settings (mutable, persisted to settings.json) ───────────────────────────

_settings = {"llm_url": "http://192.168.5.13:1234", "max_tokens": 8192, "provider": "lmstudio"}

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            _settings.update(json.load(f))
    except FileNotFoundError:
        pass

def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(_settings, f, indent=2)

def llm_base():
    return _settings["llm_url"].rstrip("/")

load_settings()

# ── Chat file helpers ─────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_path(chat_id):
    clean = re.sub(r"[^a-f0-9-]", "", chat_id)
    return os.path.join(CHATS_DIR, f"{clean}.md")

def parse_chat(content):
    meta_m = re.search(r"<!-- META\n(.*?)\n-->", content, re.DOTALL)
    msgs_m = re.search(r"<!-- MESSAGES\n(.*?)\n-->", content, re.DOTALL)
    if not meta_m:
        return None, []
    try:
        meta     = json.loads(meta_m.group(1))
        messages = json.loads(msgs_m.group(1)) if msgs_m else []
    except json.JSONDecodeError:
        return None, []
    return meta, messages

def write_chat(meta, messages):
    parts = [
        f"# {meta['title']}",
        "",
        f"> {meta['updated'][:10]}  ·  {meta.get('model', '')}",
        "",
        f"<!-- META\n{json.dumps(meta)}\n-->",
        f"<!-- MESSAGES\n{json.dumps(messages)}\n-->",
        "",
        "---",
        "",
    ]
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Assistant"
        parts += [f"**{role}**", "", msg["content"], "", "---", ""]
    return "\n".join(parts)

def list_chats():
    chats = []
    for fname in os.listdir(CHATS_DIR):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, fname), encoding="utf-8") as f:
                meta, messages = parse_chat(f.read())
            if meta:
                meta["messageCount"] = len(messages)
                chats.append(meta)
        except Exception:
            pass
    chats.sort(key=lambda c: c.get("updated", ""), reverse=True)
    return chats

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def _cors(self, code, extra=None):
        self.send_response(code)
        for k, v in CORS.items():
            self.send_header(k, v)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self._cors(code, {"Content-Type": "application/json"})
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _chat_id(self):
        m = re.search(r"/api/chats/([a-f0-9-]+)$", self.path)
        return m.group(1) if m else None

    # ── routing ──

    def do_OPTIONS(self):
        self._cors(204)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/v1/"):        self._proxy()
        elif p == "/api/chats":         self._json(200, list_chats())
        elif p == "/api/settings":      self._json(200, _settings)
        elif self._chat_id():           self._get_chat(self._chat_id())
        else:                           self._static()

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/v1/"):        self._proxy()
        elif p == "/api/chats":         self._create_chat()
        elif p == "/api/settings":      self._update_settings()
        elif p == "/api/settings/probe": self._probe()

    def do_PUT(self):
        if self._chat_id(): self._update_chat(self._chat_id())

    def do_DELETE(self):
        if self._chat_id(): self._delete_chat(self._chat_id())

    # ── settings API ──

    def _update_settings(self):
        try:
            data = json.loads(self._body())
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"}); return
        if "llm_url"    in data: _settings["llm_url"]    = data["llm_url"].rstrip("/")
        if "max_tokens" in data: _settings["max_tokens"] = int(data["max_tokens"])
        if "provider"   in data: _settings["provider"]   = data["provider"]
        save_settings()
        self._json(200, _settings)

    def _probe(self):
        """Test a candidate LLM URL without persisting it."""
        try:
            data     = json.loads(self._body())
            url      = data.get("llm_url", "").rstrip("/")
            provider = data.get("provider", "lmstudio")
        except Exception:
            self._json(400, {"error": "bad json"}); return

        # Try OpenAI-compatible /v1/models first — works for both LM Studio and Ollama 0.1.24+
        try:
            req = urllib.request.Request(
                f"{url}/v1/models",
                headers={"Authorization": "Bearer dummy"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            models = [{"id": m["id"]} for m in result.get("data", [])]
            self._json(200, {"ok": True, "models": models}); return
        except Exception:
            pass

        # Ollama fallback: native /api/tags endpoint
        if provider == "ollama":
            try:
                req = urllib.request.Request(f"{url}/api/tags")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                models = [{"id": m["name"]} for m in result.get("models", [])]
                self._json(200, {"ok": True, "models": models}); return
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)}); return

        self._json(200, {"ok": False, "error": "Could not reach host"})

    # ── chat API ──

    def _get_chat(self, chat_id):
        try:
            with open(safe_path(chat_id), encoding="utf-8") as f:
                meta, messages = parse_chat(f.read())
            self._json(200, {"meta": meta, "messages": messages})
        except FileNotFoundError:
            self._json(404, {"error": "not found"})

    def _create_chat(self):
        try:
            data = json.loads(self._body())
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"}); return
        chat_id = str(uuid.uuid4())
        ts      = now_iso()
        messages = data.get("messages", [])
        meta = {
            "id":      chat_id,
            "title":   data.get("title", "New chat"),
            "model":   data.get("model", ""),
            "created": ts,
            "updated": ts,
        }
        with open(safe_path(chat_id), "w", encoding="utf-8") as f:
            f.write(write_chat(meta, messages))
        self._json(201, meta)

    def _update_chat(self, chat_id):
        fpath = safe_path(chat_id)
        try:
            with open(fpath, encoding="utf-8") as f:
                meta, _ = parse_chat(f.read())
        except FileNotFoundError:
            self._json(404, {"error": "not found"}); return
        try:
            data = json.loads(self._body())
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"}); return
        messages = data.get("messages", [])
        if "title" in data:
            meta["title"] = data["title"]
        meta["updated"] = now_iso()
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(write_chat(meta, messages))
        self._json(200, meta)

    def _delete_chat(self, chat_id):
        try:
            os.remove(safe_path(chat_id))
            self._json(200, {"ok": True})
        except FileNotFoundError:
            self._json(404, {"error": "not found"})

    # ── static + proxy ──

    def _static(self):
        fpath = os.path.join(HERE, "index.html")
        try:
            with open(fpath, "rb") as f:
                data = f.read()
            self._cors(200, {"Content-Type": "text/html; charset=utf-8"})
            self.wfile.write(data)
        except FileNotFoundError:
            self._cors(404); self.wfile.write(b"Not found")

    def _proxy(self):
        target = llm_base() + self.path
        n      = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(n) if n else None
        req    = urllib.request.Request(
            target, data=body, method=self.command,
            headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                ct = resp.headers.get("Content-Type", "application/json")
                self._cors(resp.status, {"Content-Type": ct})
                while chunk := resp.read(4096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            self._cors(e.code, {"Content-Type": "application/json"})
            self.wfile.write(e.read())
        except Exception as e:
            self._cors(502, {"Content-Type": "application/json"})
            self.wfile.write(f'{{"error":"{e}"}}'.encode())


if __name__ == "__main__":
    server = http.server.HTTPServer(("", PORT), Handler)
    print(f"Chat app  →  http://localhost:{PORT}")
    print(f"LLM proxy →  {llm_base()}")
    print(f"Chats dir →  {CHATS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
