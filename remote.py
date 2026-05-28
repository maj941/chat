#!/usr/bin/env python3
"""Helper to talk to the optional remote agent (tools/remote_agent.py via ngrok).

Configuration lives in /home/.local_chat/remote_config.json:
  { "url": "https://xxxxx.ngrok-free.app", "token": "<bearer>" }

Set with:
  remote.py config <url> <token>
  remote.py clear
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "/home/.local_chat"
CFG_FILE = f"{BASE}/remote_config.json"


def load_cfg():
    if not os.path.exists(CFG_FILE):
        return None
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_cfg(cfg):
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_FILE)


def _req(path, method="GET", body=None):
    cfg = load_cfg()
    if not cfg or not cfg.get("url") or not cfg.get("token"):
        return {"error": "remote not configured. Use: remote.py config <url> <token>"}
    url = cfg["url"].rstrip("/") + path
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "ngrok-skip-browser-warning": "true",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        try:
            return {"error": f"HTTP {e.code}", "body": json.loads(body_text)}
        except Exception:
            return {"error": f"HTTP {e.code}", "body": body_text}
    except Exception as e:
        return {"error": str(e)}


def cmd_config(url, token):
    save_cfg({"url": url, "token": token})
    print(json.dumps({"ok": True, "url": url, "token": token[:6] + "…"}))


def cmd_clear():
    if os.path.exists(CFG_FILE):
        os.remove(CFG_FILE)
    print(json.dumps({"ok": True}))


def cmd_health():
    print(json.dumps(_req("/health"), ensure_ascii=False, indent=2))


def cmd_ls(path):
    print(json.dumps(_req("/ls", method="POST", body={"path": path}), ensure_ascii=False, indent=2))


def cmd_read(path):
    print(json.dumps(_req("/read", method="POST", body={"path": path}), ensure_ascii=False, indent=2))


def cmd_write(path, content):
    print(
        json.dumps(
            _req("/write", method="POST", body={"path": path, "content": content}),
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_shell(cmd, args):
    print(
        json.dumps(
            _req("/shell", method="POST", body={"cmd": cmd, "args": args}),
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_run_python(script, args):
    print(
        json.dumps(
            _req("/run_python", method="POST", body={"script": script, "args": args}),
            ensure_ascii=False,
            indent=2,
        )
    )


def usage():
    print(
        """Usage: remote.py <command> [args]
Commands:
  config <url> <token>           Save ngrok URL + bearer token
  clear                          Clear saved config
  health                         GET /health
  ls <path>                      POST /ls {path}
  read <path>                    POST /read {path}
  write <path> <content>         POST /write {path, content}
  shell <cmd> [args...]          POST /shell {cmd, args}  (whitelist: python,py,pytest,git,dir,type,where,tasklist,wmic)
  run_python <script> [args...]  POST /run_python {script, args}  (whitelist: tools/scan_world_objects.py, tools/object_scaner.py, launcher.py)
"""
    )


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(2)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "config":
        if len(args) < 2:
            print("config requires <url> <token>")
            sys.exit(2)
        cmd_config(args[0], args[1])
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "health":
        cmd_health()
    elif cmd == "ls":
        if not args:
            print("ls requires <path>")
            sys.exit(2)
        cmd_ls(args[0])
    elif cmd == "read":
        if not args:
            print("read requires <path>")
            sys.exit(2)
        cmd_read(args[0])
    elif cmd == "write":
        if len(args) < 2:
            print("write requires <path> <content>")
            sys.exit(2)
        cmd_write(args[0], args[1])
    elif cmd == "shell":
        if not args:
            print("shell requires <cmd> [args...]")
            sys.exit(2)
        cmd_shell(args[0], args[1:])
    elif cmd == "run_python":
        if not args:
            print("run_python requires <script> [args...]")
            sys.exit(2)
        cmd_run_python(args[0], args[1:])
    else:
        usage()
        sys.exit(2)


if __name__ == "__main__":
    main()
