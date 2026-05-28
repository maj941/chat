#!/usr/bin/env python3
"""CLI helper for Captain to interact with the local chat server.

v2: adds `wait-long <timeout>` (server-side long-poll, instant wake), `status`, `url`.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import mimetypes

BASE = "/home/.local_chat"
SERVER = "http://127.0.0.1:8765"
SEEN_FILE = f"{BASE}/seen_by_agent.json"
TOKEN_FILE = f"{BASE}/auth_token.txt"


def _load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


AUTH_TOKEN = _load_token()


def _req(path, method="GET", body=None, headers=None, timeout=10):
    url = SERVER + path
    data = None
    h = {"Accept": "application/json"}
    if AUTH_TOKEN:
        h["X-Auth-Token"] = AUTH_TOKEN
    if headers:
        h.update(headers)
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json"
        else:
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"error": str(e)}


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {"last_seen_ts": 0.0}
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_seen_ts": 0.0}


def save_seen(d):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SEEN_FILE)


def get_all_messages():
    return _req("/api/messages").get("messages", [])


def get_new_user_messages():
    seen = load_seen()
    last = float(seen.get("last_seen_ts", 0.0))
    msgs = get_all_messages()
    return [m for m in msgs if m["role"] == "user" and m["ts"] > last]


def cmd_health():
    r = _req("/health")
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_count():
    msgs = get_all_messages()
    user = sum(1 for m in msgs if m["role"] == "user")
    asst = sum(1 for m in msgs if m["role"] == "assistant")
    print(json.dumps({"total": len(msgs), "user": user, "assistant": asst}))


def cmd_peek():
    new = get_new_user_messages()
    print(f"NEW_MESSAGES: {len(new)}")
    for m in new:
        _print_message(m)


def cmd_check():
    new = get_new_user_messages()
    print(f"NEW_MESSAGES: {len(new)}")
    for m in new:
        _print_message(m)
    if new:
        seen = load_seen()
        seen["last_seen_ts"] = max(m["ts"] for m in new)
        save_seen(seen)


def _print_message(m):
    print("---")
    print(f"id: {m['id']}")
    print(f"ts: {m['ts']:.3f}  iso: {m.get('iso','')}")
    print(f"role: {m['role']}")
    if m.get("text"):
        print("text:")
        print(m["text"])
    if m.get("files"):
        print("files:")
        for f in m["files"]:
            print(f"  - {f.get('name')}  ({f.get('size')} B)  url={f.get('url')}  stored={f.get('stored')}  path={f.get('path')}")


def cmd_history(n_str):
    try:
        n = int(n_str)
    except Exception:
        n = 20
    msgs = get_all_messages()
    msgs = msgs[-n:]
    for m in msgs:
        _print_message(m)
    print(f"--- TOTAL: {len(msgs)} shown")


def cmd_send(text, role="assistant"):
    r = _req("/api/messages", method="POST", body={"text": text, "role": role})
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_send_file(path, caption=""):
    if not os.path.exists(path):
        print(json.dumps({"error": f"file not found: {path}"}))
        sys.exit(2)
    name = os.path.basename(path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = "----LocalChatBoundary" + os.urandom(8).hex()
    with open(path, "rb") as f:
        data = f.read()
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode("utf-8")
    body += f"Content-Type: {ctype}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    up = _req("/api/upload", method="POST", body=body, headers=headers, timeout=60)
    if "error" in up:
        print(json.dumps(up, ensure_ascii=False, indent=2))
        sys.exit(2)
    msg = _req(
        "/api/messages",
        method="POST",
        body={"text": caption, "files": [up], "role": "assistant"},
    )
    print(json.dumps(msg, ensure_ascii=False, indent=2))


def cmd_typing(state):
    on = state.lower() in ("on", "1", "true", "yes")
    r = _req("/api/state", method="POST", body={"agent_typing": on})
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_doing(text):
    """Set or clear current agent action shown next to the typing indicator.

    `chat.py doing "text"`  -> sets current_action
    `chat.py doing off`     -> clears current_action
    """
    if text.lower() in ("off", "clear", "-", "none", ""):
        r = _req("/api/state", method="POST", body={"current_action": ""})
    else:
        r = _req("/api/state", method="POST", body={"current_action": text, "agent_typing": True})
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_wait(timeout_str, interval_str):
    """Legacy short-polling fallback."""
    try:
        timeout = float(timeout_str)
    except Exception:
        timeout = 60.0
    try:
        interval = float(interval_str)
    except Exception:
        interval = 2.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = get_new_user_messages()
        if new:
            print(f"NEW_MESSAGES: {len(new)}")
            for m in new:
                _print_message(m)
            seen = load_seen()
            seen["last_seen_ts"] = max(m["ts"] for m in new)
            save_seen(seen)
            return
        time.sleep(interval)
    print("TIMEOUT")


def cmd_wait_long(timeout_str):
    """Server-side long-poll: one HTTP request per chunk (default 25s), instant wake."""
    try:
        total = float(timeout_str)
    except Exception:
        total = 1800.0
    deadline = time.time() + total
    chunk = 25.0
    while time.time() < deadline:
        seen = load_seen()
        since = float(seen.get("last_seen_ts", 0.0))
        remaining = deadline - time.time()
        t = min(chunk, max(1.0, remaining))
        path = f"/api/wait?since={since}&timeout={t}"
        # urlopen timeout must cover server timeout plus slack
        r = _req(path, timeout=t + 8)
        if isinstance(r, dict) and "error" in r:
            # Transient server/network — brief sleep then retry
            time.sleep(1.0)
            continue
        msgs = (r or {}).get("messages") or []
        if msgs:
            print(f"NEW_MESSAGES: {len(msgs)}")
            for m in msgs:
                _print_message(m)
            seen = load_seen()
            seen["last_seen_ts"] = max(m["ts"] for m in msgs)
            save_seen(seen)
            return
        # else: server timed out — loop and re-arm
    print("TIMEOUT")


def cmd_mark_seen():
    msgs = get_all_messages()
    user_msgs = [m for m in msgs if m["role"] == "user"]
    if user_msgs:
        seen = load_seen()
        seen["last_seen_ts"] = max(m["ts"] for m in user_msgs)
        save_seen(seen)
        print(json.dumps({"ok": True, "last_seen_ts": seen["last_seen_ts"]}))
    else:
        print(json.dumps({"ok": True, "last_seen_ts": 0}))


def cmd_url():
    r = _req("/api/url")
    print(json.dumps(r, ensure_ascii=False, indent=2))


def cmd_status():
    import subprocess

    out = {"server": _req("/health"), "url": _req("/api/url")}
    try:
        ps = subprocess.run(
            ["pgrep", "-af", "(app.py|cloudflared|watchdog.py)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out["processes"] = [l for l in (ps.stdout or "").splitlines() if l.strip()]
    except Exception as e:
        out["processes"] = [f"err: {e}"]
    msgs = get_all_messages()
    out["messages_total"] = len(msgs)
    out["last_seen_ts"] = load_seen().get("last_seen_ts", 0)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def usage():
    print(
        """Usage: chat.py <command> [args]
Commands:
  health                       Server health check
  status                       Full status (server + tunnel + processes)
  url                          Current public URL (from public_url.txt)
  check                        Print new user messages and mark them seen
  peek                         Print new user messages without marking seen
  count                        Total / user / assistant counts
  history N                    Show last N messages
  send "text"                  Send assistant message
  send-file <path> [caption]   Send file (assistant role)
  typing on|off                Toggle 'Captain typing…' indicator
  doing "text" | off           Set/clear current agent action (shown in UI)
  wait <timeout_s> <interval>  Legacy short-poll fallback
  wait-long <timeout_s>        Server long-poll (instant wake, recommended)
  mark-seen                    Mark all current user messages as seen
"""
    )


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(2)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "health":
        cmd_health()
    elif cmd == "check":
        cmd_check()
    elif cmd == "peek":
        cmd_peek()
    elif cmd == "count":
        cmd_count()
    elif cmd == "history":
        cmd_history(args[0] if args else "20")
    elif cmd == "send":
        if not args:
            print("send requires text arg")
            sys.exit(2)
        cmd_send(args[0])
    elif cmd == "send-file":
        if not args:
            print("send-file requires path")
            sys.exit(2)
        cmd_send_file(args[0], args[1] if len(args) > 1 else "")
    elif cmd == "typing":
        if not args:
            print("typing requires on|off")
            sys.exit(2)
        cmd_typing(args[0])
    elif cmd == "doing":
        if not args:
            print("doing requires <text> or 'off'")
            sys.exit(2)
        cmd_doing(" ".join(args))
    elif cmd == "wait":
        t = args[0] if len(args) > 0 else "60"
        i = args[1] if len(args) > 1 else "2"
        cmd_wait(t, i)
    elif cmd == "wait-long":
        t = args[0] if len(args) > 0 else "1800"
        cmd_wait_long(t)
    elif cmd == "mark-seen":
        cmd_mark_seen()
    elif cmd == "url":
        cmd_url()
    elif cmd == "status":
        cmd_status()
    else:
        usage()
        sys.exit(2)


if __name__ == "__main__":
    main()
