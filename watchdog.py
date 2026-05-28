#!/usr/bin/env python3
"""Supervisor for the local chat: keeps Flask + cloudflared alive.

- Restarts Flask if /health doesn't respond within 3s
- Restarts cloudflared if process is dead OR no URL has been observed for 60s
- Writes current public URL to /home/.local_chat/public_url.txt
- On URL change posts an assistant message to chat so the user sees the new link
- Designed to be launched as a background process and never exit
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = "/home/.local_chat"
URL_FILE = f"{BASE}/public_url.txt"
TUNNEL_LOG = f"{BASE}/tunnel.log"
SERVER_LOG = f"{BASE}/server.log"
WATCHDOG_LOG = f"{BASE}/watchdog.log"
TOKEN_FILE = f"{BASE}/auth_token.txt"
PAGES_URL_FILE = f"{BASE}/pages_url.txt"
PAT_FILE = f"{BASE}/github_pat.txt"
RUNTIME_REPO_FILE = f"{BASE}/runtime_repo.txt"  # contents: owner/repo (e.g. maj941/chat)
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

import base64 as _b64


def _load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _load_pages_url():
    p = os.environ.get("PAGES_URL", "").strip()
    if p:
        return p
    try:
        with open(PAGES_URL_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _load_pat():
    p = os.environ.get("GITHUB_PAT", "").strip()
    if p:
        return p
    try:
        with open(PAT_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _load_runtime_repo():
    p = os.environ.get("RUNTIME_REPO", "").strip()
    if p:
        return p
    try:
        with open(RUNTIME_REPO_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def push_runtime_to_repo(api_url, auth_token):
    """PUT runtime.json into the configured repo via GitHub Contents API.

    In multi-user mode the token is NOT shipped in the repo file: only the
    backend URL. Any client must authenticate via /api/login.
    """
    pat = _load_pat()
    repo = _load_runtime_repo()
    if not pat or not repo:
        return False, "no PAT or runtime_repo"
    payload = {"api": api_url, "ts": int(time.time())}
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    b64 = _b64.b64encode(body.encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "captain-chat-watchdog",
        "Content-Type": "application/json",
    }
    api = f"https://api.github.com/repos/{repo}/contents/runtime.json"
    # 1) get current sha if file exists
    sha = None
    try:
        r = urllib.request.Request(api, method="GET", headers=headers)
        with urllib.request.urlopen(r, timeout=10) as resp:
            cur = json.loads(resp.read().decode("utf-8"))
            sha = cur.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"get sha {e.code}: {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        return False, f"get sha err: {e}"
    # 2) put
    put_body = {
        "message": f"watchdog: update runtime backend URL ({time.strftime('%Y-%m-%d %H:%M:%S')})",
        "content": b64,
        "branch": "main",
    }
    if sha:
        put_body["sha"] = sha
    try:
        r = urllib.request.Request(
            api,
            data=json.dumps(put_body).encode("utf-8"),
            method="PUT",
            headers=headers,
        )
        with urllib.request.urlopen(r, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
            return True, j.get("commit", {}).get("sha", "")
    except urllib.error.HTTPError as e:
        return False, f"put {e.code}: {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        return False, f"put err: {e}"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\n"
    try:
        with open(WATCHDOG_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


def kill_pattern(pattern):
    try:
        subprocess.run(["pkill", "-f", pattern], timeout=5)
    except Exception:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def health_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_flask():
    log("starting flask")
    # Ensure no stragglers
    kill_pattern("python3 .*app.py")
    time.sleep(0.5)
    with open(SERVER_LOG, "ab", 0) as fh:
        p = subprocess.Popen(
            ["python3", f"{BASE}/app.py"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=BASE,
        )
    return p.pid


def start_cloudflared():
    log("starting cloudflared")
    kill_pattern("cloudflared tunnel --url http://localhost:8765")
    time.sleep(0.5)
    # Truncate tunnel log so we read a fresh URL
    try:
        open(TUNNEL_LOG, "w").close()
    except Exception:
        pass
    with open(TUNNEL_LOG, "ab", 0) as fh:
        p = subprocess.Popen(
            [
                "/tmp/cloudflared",
                "tunnel",
                "--url",
                "http://localhost:8765",
                "--protocol",
                "http2",
                "--no-autoupdate",
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return p.pid


def read_url_from_log():
    try:
        with open(TUNNEL_LOG) as f:
            data = f.read()
    except Exception:
        return None
    m = URL_RE.findall(data)
    if not m:
        return None
    # Return last one to honor reconnects
    return m[-1]


def current_recorded_url():
    if not os.path.exists(URL_FILE):
        return ""
    try:
        with open(URL_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def write_url(u):
    tmp = URL_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(u or "")
    os.replace(tmp, URL_FILE)


def post_chat(text, role="assistant"):
    try:
        body = json.dumps({"text": text, "role": role}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = _load_token()
        if token:
            headers["X-Auth-Token"] = token
        req = urllib.request.Request(
            "http://127.0.0.1:8765/api/messages",
            data=body,
            method="POST",
            headers=headers,
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log(f"post_chat err: {e}")


def cloudflared_alive():
    try:
        r = subprocess.run(
            ["pgrep", "-f", "cloudflared tunnel --url http://localhost:8765"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def main():
    log("watchdog start")
    last_known_url = current_recorded_url()
    last_url_seen_ts = 0.0
    flask_pid = None
    tunnel_pid = None
    failing_health_since = 0.0
    while True:
        # 1) Flask health
        if not health_ok():
            now = time.time()
            if failing_health_since == 0:
                failing_health_since = now
            if now - failing_health_since > 3:
                flask_pid = start_flask()
                failing_health_since = 0
                # give it a moment
                time.sleep(2)
        else:
            failing_health_since = 0

        # 2) Tunnel alive
        if not cloudflared_alive():
            tunnel_pid = start_cloudflared()
            last_known_url = ""  # force re-publish
            write_url("")
            last_url_seen_ts = time.time()
            time.sleep(5)

        # 3) Detect URL
        url = read_url_from_log()
        if url:
            last_url_seen_ts = time.time()
            token = _load_token()
            pages = _load_pages_url()
            if pages:
                pages_clean = pages.rstrip("/") + "/"
                display_url = f"{pages_clean}?api={url}&t={token}" if token else f"{pages_clean}?api={url}"
            else:
                display_url = f"{url}/?t={token}" if token else url
            if display_url != last_known_url:
                log(f"URL changed: {last_known_url!r} -> {display_url!r}")
                had_prev = bool(last_known_url)
                write_url(display_url)
                # Push runtime.json to repo (best-effort)
                ok, info = push_runtime_to_repo(url, token)
                if ok:
                    log(f"runtime.json pushed: {info}")
                else:
                    log(f"runtime.json push skipped or failed: {info}")
                if had_prev:
                    post_chat(f"⚠️ Туннель пересоздан. Новый адрес: {display_url}")
                last_known_url = display_url
        else:
            # No URL in log — if it's been > 60s and tunnel is up, recycle
            if cloudflared_alive() and time.time() - last_url_seen_ts > 60:
                log("no URL in log for 60s, restarting tunnel")
                tunnel_pid = start_cloudflared()
                last_url_seen_ts = time.time()

        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
