# Captain Chat — GitHub Pages frontend

Static frontend gateway for the Captain Chat backend. Lives at
`https://maj941.github.io/chat/` (or whatever Pages URL your repo gives).

## Setup (one time)

1. Upload `docs/index.html` (this file's sibling) to the repo root in a folder
   called `docs/`.
2. Repo Settings → Pages → Branch: `main`, Folder: `/docs` → Save.
3. Wait ~30 seconds for Pages to deploy.
4. Open `https://maj941.github.io/chat/` — you should see the gate prompt.

## Usage

When the Captain backend is up, the chat URL (which already includes the
auth token) gets posted in the system channel. Open
`https://maj941.github.io/chat/?api=<backend>&t=<token>` once — those are
remembered in `localStorage`, then the page reloads clean. From then on, your
bookmark `https://maj941.github.io/chat/` is enough.

When the tunnel restarts (every session, or after long idle), the backend URL
changes. The Captain will post the new URL inside the chat — clicking it sends
you to `https://maj941.github.io/chat/?api=<new>&t=<token>`, which updates the
stored config silently. No bookmark change needed.

If you want to clear the saved config, click `⎋ reset` in the top-right.

## CORS

The backend's `app.py` sends `Access-Control-Allow-Origin: *` on all responses
and handles `OPTIONS` preflight, so the cross-origin fetches from GitHub Pages
work without further setup.

## Security note

The token is stored in `localStorage`, so anyone with access to your browser
profile can read it. The first-time URL `?t=<token>` is a one-shot bearer; do
not paste it into public chats. To invalidate: delete `auth_token.txt` on the
backend and let the supervisor regenerate it; then propagate the new URL to
your bookmarks.
