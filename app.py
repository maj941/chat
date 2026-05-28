#!/usr/bin/env python3
"""Local chat server with multi-user support, admin, DMs, workspaces.

Compatibility layer:
- Root bearer token (auth_token.txt) still authenticates: identifies the
  system user "Captain" (system role). `chat.py` and `watchdog.py` keep
  working without change.
- Human users authenticate via /api/login → session cookie or X-Session-Token.
- Admin creates users via /api/users; users change own password via
  /api/me/password.

Roles:
- captain  -> system, posts on behalf of the agent (bearer token only)
- admin    -> full panel, delete any message, kick, manage users
- user     -> normal participant
- banned   -> can log in but cannot post or read
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    send_file,
    abort,
    make_response,
)
from werkzeug.utils import safe_join

BASE = "/home/.local_chat"
UPLOADS = f"{BASE}/uploads"
WORKSPACES = f"{BASE}/workspaces"
MSG_FILE = f"{BASE}/messages.json"
STATE_FILE = f"{BASE}/state.json"
USERS_FILE = f"{BASE}/users.json"
SESSIONS_FILE = f"{BASE}/sessions.json"
CONVS_FILE = f"{BASE}/conversations.json"
URL_FILE = f"{BASE}/public_url.txt"
TOKEN_FILE = f"{BASE}/auth_token.txt"

os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(WORKSPACES, exist_ok=True)


def _ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f)


_ensure_file(MSG_FILE, [])
_ensure_file(STATE_FILE, {"agent_typing": False, "current_action": "", "last_seen_by_agent": 0})
_ensure_file(USERS_FILE, [])
_ensure_file(SESSIONS_FILE, {})
_ensure_file(CONVS_FILE, [{"id": "c_global", "name": "Global", "type": "global", "members": []}])

if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "w") as f:
        f.write(secrets.token_urlsafe(32))
    os.chmod(TOKEN_FILE, 0o600)

with open(TOKEN_FILE) as _f:
    AUTH_TOKEN = _f.read().strip()


# ─── Locks & wake signals ──────────────────────────────────────────────
_io_lock = threading.Lock()
_user_msg_event = threading.Event()


# ─── Storage helpers ───────────────────────────────────────────────────
def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_messages():
    with open(MSG_FILE) as f:
        return json.load(f)


def save_messages(msgs):
    _atomic_write(MSG_FILE, msgs)


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    _atomic_write(STATE_FILE, state)


def load_users():
    with open(USERS_FILE) as f:
        return json.load(f)


def save_users(users):
    _atomic_write(USERS_FILE, users)


def load_sessions():
    with open(SESSIONS_FILE) as f:
        return json.load(f)


def save_sessions(s):
    _atomic_write(SESSIONS_FILE, s)


def load_convs():
    with open(CONVS_FILE) as f:
        return json.load(f)


def save_convs(c):
    _atomic_write(CONVS_FILE, c)


# ─── Password hashing (PBKDF2-SHA256, no external dep) ─────────────────
def hash_password(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw, stored):
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


# ─── Bootstrap initial users / captain ─────────────────────────────────
def _ensure_captain_user():
    users = load_users()
    if not any(u.get("role") == "captain" for u in users):
        users.append(
            {
                "id": "u_captain",
                "name": "Captain",
                "role": "captain",
                "password_hash": None,  # cannot login
                "created_at": time.time(),
            }
        )
        save_users(users)


def _ensure_initial_admin():
    """If env CAPTAIN_INITIAL_ADMIN_NAME and CAPTAIN_INITIAL_ADMIN_PASSWORD are
    set and no admin exists yet, create one."""
    users = load_users()
    if any(u.get("role") == "admin" for u in users):
        return
    name = os.environ.get("CAPTAIN_INITIAL_ADMIN_NAME", "").strip()
    pw = os.environ.get("CAPTAIN_INITIAL_ADMIN_PASSWORD", "").strip()
    if not name or not pw:
        return
    if any(u.get("name") == name for u in users):
        return
    users.append(
        {
            "id": "u_" + uuid.uuid4().hex[:10],
            "name": name,
            "role": "admin",
            "password_hash": hash_password(pw),
            "created_at": time.time(),
        }
    )
    save_users(users)


_ensure_captain_user()
_ensure_initial_admin()


# ─── Session management ────────────────────────────────────────────────
SESSION_TTL = 30 * 24 * 3600


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    sessions = load_sessions()
    now = time.time()
    sessions[token] = {"user_id": user_id, "created_at": now, "expires_at": now + SESSION_TTL}
    save_sessions(sessions)
    return token


def revoke_session(token):
    sessions = load_sessions()
    if token in sessions:
        del sessions[token]
        save_sessions(sessions)


def revoke_user_sessions(user_id):
    sessions = load_sessions()
    to_del = [t for t, s in sessions.items() if s.get("user_id") == user_id]
    for t in to_del:
        del sessions[t]
    save_sessions(sessions)
    return len(to_del)


def get_user_by_session(token):
    if not token:
        return None
    sessions = load_sessions()
    s = sessions.get(token)
    if not s:
        return None
    if s.get("expires_at", 0) < time.time():
        revoke_session(token)
        return None
    users = load_users()
    for u in users:
        if u["id"] == s["user_id"]:
            return u
    return None


# ─── Request auth ──────────────────────────────────────────────────────
def _extract_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return request.headers.get("X-Auth-Token", "").strip() or request.args.get("t", "").strip() or request.cookies.get("auth", "").strip()


def _extract_session_token():
    return (
        request.headers.get("X-Session-Token", "").strip()
        or request.args.get("s", "").strip()
        or request.cookies.get("session", "").strip()
    )


def current_actor():
    """Return (user, kind) for the current request.

    Kind is one of:
      "captain" — root bearer token; user is the system Captain user
      "user"    — valid session; user is the human user
      None      — no auth
    """
    bearer = _extract_bearer()
    if bearer and secrets.compare_digest(bearer, AUTH_TOKEN):
        users = load_users()
        for u in users:
            if u.get("role") == "captain":
                return u, "captain"
        return None, None
    sess_token = _extract_session_token()
    user = get_user_by_session(sess_token)
    if user:
        return user, "user"
    return None, None


# ─── Flask ─────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=f"{BASE}/static")


PUBLIC_PATHS = {"/health", "/api/login", "/api/runtime"}


@app.before_request
def _enforce_auth():
    if request.method == "OPTIONS":
        return make_response(("", 204))
    path = request.path or ""
    if path in PUBLIC_PATHS:
        return None
    if path == "/":
        return None  # handled below
    if path.startswith("/api/") or path.startswith("/files/"):
        user, kind = current_actor()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        # banned can only see /api/me to know they're banned
        if user.get("role") == "banned" and path not in ("/api/me", "/api/logout"):
            return jsonify({"error": "banned"}), 403
        request.actor_user = user
        request.actor_kind = kind
    return None


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token, X-Session-Token, Authorization"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ─── Root + gate page (legacy) ─────────────────────────────────────────
GATE_HTML = """<!doctype html>
<meta charset=utf-8><title>Login</title>
<style>body{background:#0b0d10;color:#e6e7ea;font-family:-apple-system,Segoe UI,Roboto,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
.box{background:#14171c;border:1px solid #262b34;padding:24px 28px;border-radius:14px;max-width:380px;width:90%}
h1{margin:0 0 12px;font-size:16px;color:#8b93a1;font-weight:600;letter-spacing:.3px;text-transform:uppercase}
input{width:100%;padding:10px;border-radius:8px;background:#1b1f26;color:#e6e7ea;border:1px solid #262b34;font:inherit;outline:none;margin:6px 0}
input:focus{border-color:#7c5cff}
button{margin-top:10px;padding:10px 14px;border:0;border-radius:8px;background:linear-gradient(135deg,#7c5cff,#5b8def);color:#fff;font-weight:600;cursor:pointer;width:100%}
.err{color:#f87171;margin-top:8px;font-size:12.5px;display:none}
</style>
<div class=box>
  <h1>Sign in</h1>
  <input id=n placeholder=username autocomplete=username>
  <input id=p type=password placeholder=password autocomplete=current-password>
  <button id=go>Sign in</button>
  <div class=err id=err>Invalid credentials.</div>
</div>
<script>
async function go(){
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('n').value.trim(),password:document.getElementById('p').value})});
  if (r.ok) { location.reload(); } else { document.getElementById('err').style.display='block'; }
}
document.getElementById('go').onclick=go;
document.querySelectorAll('input').forEach(i=>i.addEventListener('keydown',e=>{if(e.key==='Enter')go();}));
</script>
"""


@app.route("/")
def index():
    # If root bearer ?t= → set cookie + redirect to clean /
    qs_token = request.args.get("t", "")
    if qs_token and secrets.compare_digest(qs_token, AUTH_TOKEN):
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        resp.set_cookie("auth", AUTH_TOKEN, max_age=60 * 60 * 24 * 90, httponly=False, samesite="Lax")
        return resp
    # Check if already auth'd via session
    user, kind = current_actor()
    if user:
        return send_from_directory(BASE, "index.html")
    return GATE_HTML


# ─── Login / Logout / Me ───────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    pw = data.get("password") or ""
    if not name or not pw:
        return jsonify({"error": "missing credentials"}), 400
    users = load_users()
    user = next((u for u in users if u.get("name") == name and u.get("password_hash")), None)
    if not user or not check_password(pw, user["password_hash"]):
        return jsonify({"error": "invalid"}), 401
    token = create_session(user["id"])
    resp = jsonify({"ok": True, "user": _public_user(user), "session": token})
    resp.set_cookie(
        "session", token, max_age=SESSION_TTL, httponly=True, samesite="Lax", secure=True
    )
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    token = _extract_session_token()
    if token:
        revoke_session(token)
    resp = jsonify({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.route("/api/me", methods=["GET"])
def me():
    return jsonify({"user": _public_user(request.actor_user)})


@app.route("/api/me/password", methods=["POST"])
def change_my_password():
    data = request.get_json(silent=True) or {}
    old = data.get("old") or ""
    new = data.get("new") or ""
    if not new or len(new) < 6:
        return jsonify({"error": "new password too short"}), 400
    users = load_users()
    me_user = next((u for u in users if u["id"] == request.actor_user["id"]), None)
    if not me_user:
        return jsonify({"error": "not found"}), 404
    # Captain has no password
    if not me_user.get("password_hash"):
        return jsonify({"error": "cannot change"}), 400
    if not check_password(old, me_user["password_hash"]):
        return jsonify({"error": "wrong old password"}), 401
    me_user["password_hash"] = hash_password(new)
    save_users(users)
    return jsonify({"ok": True})


def _public_user(u):
    if not u:
        return None
    return {
        "id": u["id"],
        "name": u["name"],
        "role": u["role"],
        "created_at": u.get("created_at"),
    }


# ─── User management (admin) ───────────────────────────────────────────
def _require_admin():
    if not request.actor_user or request.actor_user.get("role") != "admin":
        return jsonify({"error": "admin only"}), 403


@app.route("/api/users", methods=["GET"])
def list_users():
    err = _require_admin()
    if err:
        return err
    users = load_users()
    return jsonify({"users": [_public_user(u) for u in users]})


@app.route("/api/users", methods=["POST"])
def create_user():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    pw = data.get("password") or ""
    role = (data.get("role") or "user").strip()
    if role not in ("user", "admin", "banned"):
        return jsonify({"error": "bad role"}), 400
    if not name or not pw or len(pw) < 6:
        return jsonify({"error": "name + password>=6 required"}), 400
    users = load_users()
    if any(u["name"] == name for u in users):
        return jsonify({"error": "name taken"}), 409
    new_user = {
        "id": "u_" + uuid.uuid4().hex[:10],
        "name": name,
        "role": role,
        "password_hash": hash_password(pw),
        "created_at": time.time(),
    }
    users.append(new_user)
    save_users(users)
    return jsonify({"user": _public_user(new_user)})


@app.route("/api/users/<user_id>", methods=["PATCH"])
def edit_user(user_id):
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    users = load_users()
    u = next((x for x in users if x["id"] == user_id), None)
    if not u:
        return jsonify({"error": "not found"}), 404
    if u.get("role") == "captain":
        return jsonify({"error": "cannot edit captain"}), 400
    if "role" in data:
        if data["role"] not in ("user", "admin", "banned"):
            return jsonify({"error": "bad role"}), 400
        u["role"] = data["role"]
    if "password" in data and data["password"]:
        if len(data["password"]) < 6:
            return jsonify({"error": "password too short"}), 400
        u["password_hash"] = hash_password(data["password"])
        revoke_user_sessions(user_id)
    if "name" in data and data["name"]:
        if any(x["name"] == data["name"] and x["id"] != user_id for x in users):
            return jsonify({"error": "name taken"}), 409
        u["name"] = data["name"]
    save_users(users)
    return jsonify({"user": _public_user(u)})


@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    err = _require_admin()
    if err:
        return err
    users = load_users()
    u = next((x for x in users if x["id"] == user_id), None)
    if not u:
        return jsonify({"error": "not found"}), 404
    if u.get("role") == "captain":
        return jsonify({"error": "cannot delete captain"}), 400
    if u["id"] == request.actor_user["id"]:
        return jsonify({"error": "cannot delete self"}), 400
    users.remove(u)
    save_users(users)
    revoke_user_sessions(user_id)
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>/kick", methods=["POST"])
def kick_user(user_id):
    err = _require_admin()
    if err:
        return err
    n = revoke_user_sessions(user_id)
    return jsonify({"ok": True, "sessions_revoked": n})


# ─── Conversations (Global + DMs) ──────────────────────────────────────
def _user_can_see_conv(user, conv):
    if not user:
        return False
    if conv["type"] == "global":
        return True
    if conv["type"] == "dm":
        return user["id"] in conv.get("members", [])
    return False


def _ensure_global():
    convs = load_convs()
    if not any(c["type"] == "global" for c in convs):
        convs.insert(0, {"id": "c_global", "name": "Global", "type": "global", "members": []})
        save_convs(convs)


@app.route("/api/conversations", methods=["GET"])
def list_convs():
    convs = load_convs()
    user = request.actor_user
    visible = [c for c in convs if _user_can_see_conv(user, c)]
    return jsonify({"conversations": visible})


@app.route("/api/conversations", methods=["POST"])
def create_conv():
    data = request.get_json(silent=True) or {}
    other_id = data.get("with") or ""
    if not other_id:
        return jsonify({"error": "with required"}), 400
    users = load_users()
    other = next((u for u in users if u["id"] == other_id), None)
    if not other:
        return jsonify({"error": "user not found"}), 404
    if other["id"] == request.actor_user["id"]:
        return jsonify({"error": "cannot DM self"}), 400
    convs = load_convs()
    # idempotent: return existing
    member_set = {request.actor_user["id"], other_id}
    for c in convs:
        if c["type"] == "dm" and set(c.get("members", [])) == member_set:
            return jsonify({"conversation": c})
    new = {
        "id": "c_" + uuid.uuid4().hex[:10],
        "name": f"DM {request.actor_user['name']} ↔ {other['name']}",
        "type": "dm",
        "members": [request.actor_user["id"], other_id],
        "created_at": time.time(),
    }
    convs.append(new)
    save_convs(convs)
    return jsonify({"conversation": new})


# ─── Messages ──────────────────────────────────────────────────────────
def _filter_msgs_for_user(msgs, user, conv_id=None):
    convs = {c["id"]: c for c in load_convs()}
    out = []
    for m in msgs:
        cid = m.get("conv_id", "c_global")
        if conv_id and cid != conv_id:
            continue
        c = convs.get(cid)
        if not c:
            continue
        if not _user_can_see_conv(user, c):
            continue
        out.append(m)
    return out


@app.route("/api/messages", methods=["GET"])
def get_messages():
    since = float(request.args.get("since", "0"))
    conv_id = request.args.get("conv_id") or None
    msgs = load_messages()
    msgs = _filter_msgs_for_user(msgs, request.actor_user, conv_id)
    if since > 0:
        msgs = [m for m in msgs if m["ts"] > since]
    state = load_state()
    return jsonify(
        {
            "messages": msgs,
            "agent_typing": state.get("agent_typing", False),
            "current_action": state.get("current_action", ""),
        }
    )


@app.route("/api/messages", methods=["POST"])
def post_message():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    files = data.get("files") or []
    conv_id = (data.get("conv_id") or "c_global").strip()
    if not text and not files:
        return jsonify({"error": "empty"}), 400
    convs = {c["id"]: c for c in load_convs()}
    conv = convs.get(conv_id)
    if not conv:
        return jsonify({"error": "conv not found"}), 404
    if not _user_can_see_conv(request.actor_user, conv):
        return jsonify({"error": "forbidden"}), 403
    role = request.actor_user["role"]
    # backwards-compat: legacy 'role' field on message (assistant/user)
    legacy_role = "user"
    if role == "captain":
        legacy_role = "assistant"
    msg = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "user_id": request.actor_user["id"],
        "username": request.actor_user["name"],
        "role": legacy_role,  # for old UI compat
        "user_role": role,
        "conv_id": conv_id,
        "text": text,
        "files": files,
        "iso": datetime.now().isoformat(timespec="seconds"),
    }
    with _io_lock:
        msgs = load_messages()
        msgs.append(msg)
        save_messages(msgs)
    if role in ("user", "admin", "banned"):
        _user_msg_event.set()
        threading.Timer(0.05, _user_msg_event.clear).start()
    return jsonify(msg)


@app.route("/api/messages/<msg_id>", methods=["DELETE"])
def delete_message(msg_id):
    msgs = load_messages()
    m = next((x for x in msgs if x.get("id") == msg_id), None)
    if not m:
        return jsonify({"error": "not found"}), 404
    role = request.actor_user["role"]
    if role != "admin" and m.get("user_id") != request.actor_user["id"]:
        return jsonify({"error": "forbidden"}), 403
    msgs.remove(m)
    save_messages(msgs)
    return jsonify({"ok": True})


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
    conv_id = request.args.get("conv_id") or None
    deadline = time.time() + timeout
    while True:
        msgs = load_messages()
        # For Captain (root bearer): any non-captain msg counts as "new user"
        if request.actor_kind == "captain":
            new = [m for m in msgs if m.get("user_role") in ("user", "admin") and m["ts"] > since]
        else:
            visible = _filter_msgs_for_user(msgs, request.actor_user, conv_id)
            new = [m for m in visible if m.get("user_id") != request.actor_user["id"] and m["ts"] > since]
        if new:
            state = load_state()
            return jsonify(
                {
                    "messages": new,
                    "agent_typing": state.get("agent_typing", False),
                    "current_action": state.get("current_action", ""),
                    "timed_out": False,
                }
            )
        remaining = deadline - time.time()
        if remaining <= 0:
            state = load_state()
            return jsonify(
                {
                    "messages": [],
                    "agent_typing": state.get("agent_typing", False),
                    "current_action": state.get("current_action", ""),
                    "timed_out": True,
                }
            )
        _user_msg_event.wait(timeout=min(remaining, 30.0))


# ─── Uploads (per-user folder) ─────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    user_id = request.actor_user["id"]
    user_dir = os.path.join(UPLOADS, user_id)
    os.makedirs(user_dir, exist_ok=True)
    safe = f.filename.replace("/", "_").replace("\\", "_")
    name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_{safe}"
    path = os.path.join(user_dir, name)
    f.save(path)
    size = os.path.getsize(path)
    ctype = f.mimetype or "application/octet-stream"
    return jsonify(
        {
            "name": f.filename,
            "stored": f"{user_id}/{name}",
            "size": size,
            "type": ctype,
            "url": f"/files/{user_id}/{name}",
            "path": path,
            "owner_id": user_id,
        }
    )


@app.route("/files/<path:rel>")
def files(rel):
    # rel is "<user_id>/<name>" or legacy "<name>"
    parts = rel.split("/", 1)
    if len(parts) == 2:
        owner_id, name = parts
        # Check ownership: actor must be admin, captain, or owner
        actor_role = request.actor_user["role"]
        if actor_role not in ("admin", "captain") and request.actor_user["id"] != owner_id:
            return jsonify({"error": "forbidden"}), 403
        target = safe_join(UPLOADS, owner_id, name)
    else:
        # Legacy unscoped upload — only admin/captain
        actor_role = request.actor_user["role"]
        if actor_role not in ("admin", "captain"):
            return jsonify({"error": "forbidden"}), 403
        target = safe_join(UPLOADS, parts[0])
    if not target or not os.path.exists(target):
        abort(404)
    return send_file(target)


# ─── Workspaces (per-user file ops) ────────────────────────────────────
def _user_ws(user_id):
    p = os.path.join(WORKSPACES, user_id)
    os.makedirs(p, exist_ok=True)
    return p


def _ws_target(user_id, rel):
    ws = _user_ws(user_id)
    target = safe_join(ws, rel) if rel else ws
    return target


@app.route("/api/workspace/list", methods=["GET"])
def ws_list():
    user = request.actor_user
    target_user_id = request.args.get("user_id") or user["id"]
    if target_user_id != user["id"] and user["role"] not in ("admin", "captain"):
        return jsonify({"error": "forbidden"}), 403
    rel = request.args.get("path") or ""
    target = _ws_target(target_user_id, rel)
    if not target or not os.path.exists(target):
        return jsonify({"entries": []})
    if os.path.isfile(target):
        return jsonify({"entries": [{"name": os.path.basename(target), "type": "file", "size": os.path.getsize(target)}]})
    entries = []
    for n in sorted(os.listdir(target)):
        full = os.path.join(target, n)
        if os.path.isdir(full):
            entries.append({"name": n, "type": "dir"})
        else:
            entries.append({"name": n, "type": "file", "size": os.path.getsize(full)})
    return jsonify({"entries": entries, "path": rel})


@app.route("/api/workspace/read", methods=["GET"])
def ws_read():
    user = request.actor_user
    target_user_id = request.args.get("user_id") or user["id"]
    if target_user_id != user["id"] and user["role"] not in ("admin", "captain"):
        return jsonify({"error": "forbidden"}), 403
    rel = request.args.get("path") or ""
    target = _ws_target(target_user_id, rel)
    if not target or not os.path.exists(target) or not os.path.isfile(target):
        return jsonify({"error": "not found"}), 404
    if os.path.getsize(target) > 5 * 1024 * 1024:
        return jsonify({"error": "too large"}), 413
    try:
        with open(target, "rb") as f:
            data = f.read()
        try:
            return jsonify({"content": data.decode("utf-8"), "binary": False})
        except UnicodeDecodeError:
            return jsonify({"content": base64.b64encode(data).decode(), "binary": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workspace/write", methods=["POST"])
def ws_write():
    user = request.actor_user
    data = request.get_json(silent=True) or {}
    target_user_id = data.get("user_id") or user["id"]
    if target_user_id != user["id"] and user["role"] not in ("admin", "captain"):
        return jsonify({"error": "forbidden"}), 403
    rel = data.get("path") or ""
    if not rel:
        return jsonify({"error": "path required"}), 400
    target = _ws_target(target_user_id, rel)
    if not target:
        return jsonify({"error": "bad path"}), 400
    os.makedirs(os.path.dirname(target), exist_ok=True)
    content = data.get("content") or ""
    is_b64 = bool(data.get("binary"))
    try:
        with open(target, "wb") as f:
            if is_b64:
                f.write(base64.b64decode(content))
            else:
                f.write(content.encode("utf-8"))
        return jsonify({"ok": True, "size": os.path.getsize(target)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workspace/delete", methods=["POST"])
def ws_delete():
    user = request.actor_user
    data = request.get_json(silent=True) or {}
    target_user_id = data.get("user_id") or user["id"]
    if target_user_id != user["id"] and user["role"] not in ("admin", "captain"):
        return jsonify({"error": "forbidden"}), 403
    rel = data.get("path") or ""
    target = _ws_target(target_user_id, rel)
    if not target or not os.path.exists(target):
        return jsonify({"error": "not found"}), 404
    try:
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        else:
            os.remove(target)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── State (typing + current_action) ──────────────────────────────────
@app.route("/api/state", methods=["GET", "POST"])
def state():
    if request.method == "POST":
        # Only Captain can change typing/current_action
        if request.actor_user.get("role") != "captain":
            return jsonify({"error": "captain only"}), 403
        data = request.get_json(silent=True) or {}
        st = load_state()
        st.update(data)
        save_state(st)
        return jsonify(st)
    return jsonify(load_state())


@app.route("/api/clear", methods=["POST"])
def clear():
    err = _require_admin()
    if err:
        return err
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


@app.route("/api/runtime", methods=["GET"])
def runtime_info():
    """Public minimal info for the Pages gateway to bootstrap."""
    url = ""
    if os.path.exists(URL_FILE):
        try:
            with open(URL_FILE) as f:
                url = f.read().strip()
        except Exception:
            pass
    return jsonify({"url": url, "auth_required": True, "multi_user": True})


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": time.time(), "messages": len(load_messages())})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
