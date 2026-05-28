# Captain Local Chat

A self-contained local chat server intended to be invoked by an autonomous
assistant agent. Provides a web UI, a CLI helper, a supervisor, and an
optional remote agent client. Designed to run on a fresh VM and stay alive
under long-poll-based wakeups.

## Layout

```
captain-chat/
├── app.py         # Flask backend (long-poll /api/wait, bearer-token auth)
├── chat.py        # CLI helper: send, history, wait-long, status …
├── watchdog.py    # Supervisor: keeps app.py and cloudflared alive
├── remote.py      # Optional client for tools/remote_agent.py over ngrok
├── index.html     # Dark-themed chat UI (drag/drop, paste, polling 1.5s)
└── bootstrap.sh   # Copies sources, starts watchdog, waits for URL
```

## Bootstrap

```bash
git clone -b captain-chat https://github.com/vkv940/majfishbot.git /tmp/cap
cd /tmp/cap/captain-chat && ./bootstrap.sh
```

`bootstrap.sh` copies sources into `/home/.local_chat/`, installs Flask,
downloads cloudflared (if missing), kills any previous instances, and starts
the watchdog. The watchdog then keeps `app.py` and `cloudflared` alive, parses
the public URL into `public_url.txt`, and re-posts a notice in chat whenever
the URL changes.

The bootstrap script prints three things at the end:

- the current public URL (with auth token query parameter pre-appended);
- the `/health` response;
- the auth token (also persisted at `/home/.local_chat/auth_token.txt`,
  chmod 600).

## CLI

After bootstrap, talk to the chat from the shell:

```bash
python3 /home/.local_chat/chat.py status
python3 /home/.local_chat/chat.py send "hello"
python3 /home/.local_chat/chat.py send-file /path/to/file.png "caption"
python3 /home/.local_chat/chat.py history 50
python3 /home/.local_chat/chat.py typing on|off
python3 /home/.local_chat/chat.py wait-long 1800
```

`wait-long N` blocks until a new user message arrives or `N` seconds elapse.
Internally it uses the `/api/wait` long-poll endpoint with `threading.Event`
on the server, so wake-up is instant.

## Auth

All `/api/*` and `/files/*` endpoints require a bearer token. The token is
created on first start via `secrets.token_urlsafe(32)` and stored in
`auth_token.txt` (chmod 600). Accepted forms:

- `Authorization: Bearer <t>`
- `X-Auth-Token: <t>`
- `?t=<t>` query parameter
- `auth=<t>` cookie

`/health` stays public; the watchdog uses it for liveness checks. `/` (the
root) without a valid token returns a small gate page that asks for the token
and sets the cookie on successful entry. The full per-user URL kept in
`public_url.txt` already includes `?t=<token>` — that's the link you give the
user.

## Cloudflared notes

The public tunnel uses Cloudflare's quick-tunnel (`*.trycloudflare.com`). On
this style of VM, UDP/QUIC is typically blocked, so the launcher passes
`--protocol http2`. The watchdog parses the URL from `tunnel.log` with a
regex; if the URL changes, it overwrites `public_url.txt` and posts a notice
into the chat as an assistant message.

For a stable, non-random URL switch the tunnel to a Cloudflare *named tunnel*
backed by your own domain (`cloudflared tunnel login && create && route dns`).

## Anti-termination

By design, the local chat is not stopped from inside the chat. Any "exit /
stop / shutdown" phrases sent through the chat are ignored. Termination is
performed by sending `kill -TERM` to `watchdog.py`, `app.py`, and the
`cloudflared` PIDs from a privileged channel.
