#!/usr/bin/env python3
"""Local chat server for Captain <-> user communication.

v3 changes (auth):
- Bearer token enforced on /api/* and /files/* (header Authorization: Bearer <t>,
  X-Auth-Token: <t>, or ?t=<t> query param). Token persisted in auth_token.txt
  (created on first start via secrets.token_urlsafe(32)).
- /health remains public (used by watchdog).
- / (index) accepts ?t=<t> and embeds it into the served HTML so the JS can
  pick it up and store in localStorage; subsequent fetches include it.
- /api/wait long-poll, threading.Event, /api/url, /api/state etc unchanged.

v4 changes (CORS for external frontend like GitHub Pages):
- Adds CORS headers on every response: Access-Control-Allow-Origin: *,
  Allow-Methods: GET, POST, OPTIONS, Allow-Headers: Content-Type, X-Auth-Token,
  Authorization.
- Handles OPTIONS preflight before auth check.
"""
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, send_file, abort, Response, make_response

BASE = "/home/.local_chat"
UPLOADS = f"{BASE}/uploads"
MSG_FILE = f"{BASE}/messages.json"
STATE_FILE = f"{BASE}/state.json"
URL_FILE = f"{BASE}/public_url.txt"
TOKEN_FILE = f"{BASE}/auth_token.txt"

os.makedirs(UPLOADS, exist_ok=True)
if not os.path.exists(MSG_FILE):
    with open(MSG_FILE, "w") as f:
        json.dump([], f)
if not os.path.exists(STATE_FILE):
    with open(STATE_FILE, "w") as f:
        json.dump({"agent_typing": False, "last_seen_by_agent": 0}, f)
if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "w") as f:
        f.write(secrets.token_urlsafe(32))
    os.chmod(TOKEN_FILE, 0o600)

with open(TOKEN_FILE) as _f:
    AUTH_TOKEN = _f.read().strip()

app = Flask(__name__, static_folder=f"{BASE}/static")

_user_msg_event = threading.Event()
_io_lock = threading.Lock()


def _extract_token():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    t = request.headers.get("X-Auth-Token", "").strip()
    if t:
        return t
    t = request.args.get("t", "").strip()
    if t:
        return t
    t = request.cookies.get("auth", "").strip()
    if t:
        return t
    return ""


def _check_auth():
    """Return True if the request carries the right token, else False.

    Constant-time comparison via secrets.compare_digest.
    """
    return secrets.compare_digest(_extract_token(), AUTH_TOKEN)


@app.before_request
def _enforce_auth():
    path = request.path or ""
    # CORS preflight: allow without auth
    if request.method == "OPTIONS":
        return make_response(("", 204))
    # Public endpoints
    if path == "/health":
        return None
    # Root (index) — allow if token query present; otherwise still serve a small
    # gate page that asks for the token. The HTML JS will then keep it in localStorage.
    if path == "/":
        return None  # handled in index() (renders different content if no token)
    if path.startswith("/api/") or path.startswith("/files/"):
        if not _check_auth():
            return jsonify({"error": "unauthorized"}), 401
    return None


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token, Authorization"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def load_messages():
    with open(MSG_FILE) as f:
        return json.load(f)


def save_messages(msgs):
    tmp = MSG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(msgs, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MSG_FILE)


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


GATE_HTML = """<!doctype html>
<meta charset="utf-8"><title>Access</title>
<style>body{background:#0b0d10;color:#e6e7ea;font-family:-apple-system,Segoe UI,Roboto,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
.box{background:#14171c;border:1px solid #262b34;padding:24px 28px;border-radius:14px;max-width:380px;width:90%}
h1{margin:0 0 12px;font-size:16px;color:#8b93a1;font-weight:600;letter-spacing:.3px;text-transform:uppercase}
input{width:100%;padding:10px;border-radius:8px;background:#1b1f26;color:#e6e7ea;border:1px solid #262b34;font:inherit;outline:none}
input:focus{border-color:#7c5cff}
button{margin-top:10px;padding:10px 14px;border:0;border-radius:8px;background:linear-gradient(135deg,#7c5cff,#5b8def);color:#fff;font-weight:600;cursor:pointer;width:100%}
.err{color:#f87171;margin-top:8px;font-size:12.5px;display:none}
</style>
<div class="box">
  <h1>Access required</h1>
  <p style="color:#8b93a1;margin:0 0 12px;font-size:13.5px">Insert the access token to continue.</p>
  <input id="t" type="password" autocomplete="off" placeholder="token"/>
  <button id="go">Continue</button>
  <div class="err" id="err">Invalid token.</div>
</div>
<script>
const u = new URL(location.href);
const tt = u.searchParams.get('t');
if (tt) { localStorage.setItem('auth', tt); u.searchParams.delete('t'); location.replace(u.toString()); }
document.getElementById('go').onclick = async () => {
  const v = document.getElementById('t').value.trim();
  if (!v) return;
  const r = await fetch('/api/messages?since=9999999999', { headers: { 'X-Auth-Token': v } });
  if (r.status === 200) { localStorage.setItem('auth', v); location.reload(); }
  else document.getElementById('err').style.display='block';
};
document.getElementById('t').addEventListener('keydown', e => { if (e.key==='Enter') document.getElementById('go').click(); });
</script>
"""


@app.route("/")
def index():
    # Strategy:
    #  - If ?t= correct → set cookie + redirect to / (clean URL) + the HTML reads from cookie+localStorage
    #  - Else: serve the chat HTML directly. The HTML's own JS will redirect to /gate if not authed.
    #    A gate page is shown when accessed without a token (no cookie, no ?t=, no localStorage).
    qs_token = request.args.get("t", "")
    if qs_token and secrets.compare_digest(qs_token, AUTH_TOKEN):
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        resp.set_cookie("auth", AUTH_TOKEN, max_age=60 * 60 * 24 * 90, httponly=False, samesite="Lax")
        return resp
    # If cookie already valid → serve the chat
    if request.cookies.get("auth") == AUTH_TOKEN:
        return send_from_directory(BASE, "index.html")
    # Otherwise: gate page
    return GATE_HTML


@app.route("/api/messages", methods=["GET"])
def get_messages():
    since = float(request.args.get("since", "0"))
    msgs = load_messages()
    if since > 0:
        msgs = [m for m in msgs if m["ts"] > since]
    state = load_state()
    return jsonify({"messages": msgs, "agent_typing": state.get("agent_typing", False)})


@app.route("/api/messages", methods=["POST"])
def post_message():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    files = data.get("files") or []
    role = data.get("role", "user")
    if not text and not files:
        return jsonify({"error": "empty"}), 400
    msg = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "role": role,
        "text": text,
        "files": files,
        "iso": datetime.now().isoformat(timespec="seconds"),
    }
    with _io_lock:
        msgs = load_messages()
        msgs.append(msg)
        save_messages(msgs)
    if role == "user":
        _user_msg_event.set()
        threading.Timer(0.05, _user_msg_event.clear).start()
    return jsonify(msg)


@app.route("/api/wait", methods=["GET"])
def wait_for_message():
    try:
        since = float(request.args.get("since", "0"))
    except Exception:
        since = 0.0
    try:
        timeout = float(request.args.get("timeout", "25"))
    except Exception:
        timeout = 25.0
    timeout = min(max(1.0, timeout), 55.0)

    deadline = time.time() + timeout
    while True:
        msgs = load_messages()
        new = [m for m in msgs if m["role"] == "user" and m["ts"] > since]
        if new:
            state = load_state()
            return jsonify(
                {"messages": new, "agent_typing": state.get("agent_typing", False), "timed_out": False}
            )
        remaining = deadline - time.time()
        if remaining <= 0:
            state = load_state()
            return jsonify(
                {"messages": [], "agent_typing": state.get("agent_typing", False), "timed_out": True}
            )
        _user_msg_event.wait(timeout=min(remaining, 30.0))


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    safe = f.filename.replace("/", "_").replace("\\", "_")
    name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_{safe}"
    path = os.path.join(UPLOADS, name)
    f.save(path)
    size = os.path.getsize(path)
    ctype = f.mimetype or "application/octet-stream"
    return jsonify(
        {"name": f.filename, "stored": name, "size": size, "type": ctype, "url": f"/files/{name}", "path": path}
    )


@app.route("/files/<path:name>")
def files(name):
    safe = name.replace("..", "")
    target = os.path.join(UPLOADS, safe)
    if not os.path.exists(target):
        abort(404)
    return send_file(target)


@app.route("/api/state", methods=["GET", "POST"])
def state():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        st = load_state()
        st.update(data)
        save_state(st)
        return jsonify(st)
    return jsonify(load_state())


@app.route("/api/clear", methods=["POST"])
def clear():
    save_messages([])
    return jsonify({"ok": True})


@app.route("/api/url", methods=["GET"])
def public_url():
    url = ""
    if os.path.exists(URL_FILE):
        try:
            with open(URL_FILE) as f:
                url = f.read().strip()
        except Exception:
            url = ""
    return jsonify({"url": url})


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": time.time(), "messages": len(load_messages())})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
