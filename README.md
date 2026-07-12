# x-technews-scraper

Turn your personal X (Twitter) feed into clean tech-news articles.

A small, dependency-light pipeline that:
1. **Scrapes** the X app on an Android phone (real logged-in app — no X API needed).
2. **Rewrites** the posts into tech-news articles with an LLM.
3. Outputs **Markdown** (easy to turn into a site / newsletter later).

```
PHONE (X app, logged in)
   │  uiautomator dump + input swipe/tap   (runs on-device via Termux,
   │                                           or from a host via adb)
   ▼
extract.py  ──►  raw/tweets_<ts>.json
   │
   ▼
rewrite.py   ──►  articles/<ts>_<slug>.md  +  articles/index.md
   (LLM: mistral-large via OpenAI-compatible router, exponential backoff)
```

## Features
- Pulls the **"For you"** tab.
- **Skips ads** (Promoted / Sponsored / Advertisement).
- De-duplicates posts.
- Filters to tech-relevant posts and groups them into stories.
- *(Optional, experimental)* comment scraping — see [Notes](#4-notes--limitations).

---

## 1. Requirements
- Android phone with the **X app installed and logged in**.
- **Termux** (F-Droid) with SSH, or any machine with `adb` + Python 3.
- Python deps: `requests` (rewriter) and `zeroconf` (optional; enables
  automatic discovery of the phone's changing wireless-debugging port).
- The phone must allow UI automation (`input` / `uiautomator`). On a Pixel this
  works from Termux directly once the app is open.

## 2. Setup

> **Recommended: run from a laptop** (host mode) with the phone connected over
> USB (`adb usb`) or a pinned wireless port (`adb tcpip 5555`). Termux *can* work
> but its app sandbox **cannot** run `input` / `uiautomator` / `monkey` directly
> (no `INJECT_EVENTS`), so it must instead drive the phone's own adbd at
> `127.0.0.1:<port>`. Host mode is simpler and more reliable.

### Laptop (host) mode — recommended
```bash
pip install requests zeroconf
git clone <repo-url> x_scrapper && cd x_scrapper
cp .env.example .env                 # add your XS_API_KEY
adb connect 100.91.248.110:5555     # see "pinned port" tip below
python3 extract.py 8 && python3 rewrite.py
```
The script auto-discovers the phone via mDNS and falls back to `:5555`, so a
plain `python3 extract.py 8` usually just works once the phone is connected.

### Termux (on-device) mode
Termux's sandbox can't run `input`/`uiautomator`/`monkey` directly. Install
`adb` in Termux and drive the phone's *own* wireless-debugging adbd:
```bash
pkg update && pkg install python git android-tools
pip install requests zeroconf
termux-setup-storage
export XS_ADB_PORT=<conn-port>       # e.g. 35111 from Wireless debugging
adb connect 127.0.0.1:<conn-port>
```

```bash
pkg update && pkg install python git android-tools
pip install requests zeroconf
termux-setup-storage                      # grant storage permission
git clone <repo-url> x_scrapper
cd x_scrapper
cp .env.example .env                      # add your XS_API_KEY
```

Enable **Developer options → Wireless debugging** on the phone, then pair &
connect adb to the device itself (split-screen Settings + Termux helps):
```bash
adb pair 127.0.0.1:<pair-port> <code>    # 6-digit code from Wireless debugging
adb connect 127.0.0.1:<conn-port>        # "IP address & Port" shown in settings
export XS_ADB_PORT=<conn-port>           # e.g. 35111
adb devices                              # should list 127.0.0.1:<conn-port>
```
Pairing persists; the connection drops on reboot / screen lock (re-run
`adb connect`). Put the `export XS_ADB_PORT=...` in your shell rc for 24/7.

Open the X app, logged in, on the "For you" tab, then:
```bash
python3 extract.py 8        # scrape 8 scrolls of the feed
python3 rewrite.py          # turn the latest scrape into articles
```
The script auto-wakes the screen and runs `svc power stayon true`, so keep the
phone **plugged in**. Ensure X is the foreground app (no screen lock) — if the
screen is off/locked, `uiautomator`/`wm` can't find a window and the run fails.
Articles land in `articles/` with an `index.md`.

### Host mode (run from a laptop — recommended)
The most reliable setup is **USB**: plug the phone in and adb stays connected
(no drops, no port changes).

For **wireless**, the Wireless-debugging port randomises every session (e.g.
yesterday `:35111`, today `:43921`). The script **auto-discovers the current
port via mDNS**, so you don't need to track it:

```bash
pip install zeroconf          # enables automatic port discovery
python3 extract.py 8 && python3 rewrite.py
```
- If `zeroconf` isn't installed (or mDNS is blocked on your network), set the
  port manually: `export XS_DEVICE=192.168.x.x:43921` (from the phone's
  Wireless debugging "IP address & Port").
- If the connection drops mid-run, `ensure_connected()` re-runs `adb connect`
  and, if that still fails, **rediscovers the new port over mDNS** before each
  scroll.

Prefer a fixed port? Pin one with `adb tcpip 5555` (once, over USB), then
`adb connect 192.168.x.x:5555` — it survives normal use (resets only on reboot).

Tips for stability:
- Give the phone a **static IP** (or reserve one in your router) so the address
  doesn't change.
- Keep the screen **on and unlocked** (the script runs `svc power stayon true`
  and wakes it, but a secure lock will still block `uiautomator`).

`ON_DEVICE` is auto-detected via Termux's `PREFIX`; from a laptop it defaults to
host mode.

## 3. Configuration
Secrets live in a `.env` file (gitignored). Copy the template and fill in:
```bash
cp .env.example .env
# edit .env: set XS_API_KEY, optionally XS_BASE_URL / XS_MODEL
```
`rewrite.py` reads these via env (`XS_API_KEY`, `XS_BASE_URL`, `XS_MODEL`).
You may also export them in the shell instead of using `.env`.

`extract.py` settings (env vars, no code edits needed):
```bash
XS_DEVICE=100.91.248.110:5555   # override auto-detected target (host mode)
XS_ADB_PORT=35111                # Termux self-adb port (ON_DEVICE only)
XS_COMMENT_TOP=0                 # tweets to try comment-scraping on (0=off;
                                 # the current X app rarely exposes replies)
```
`ON_DEVICE` is auto-detected via Termux's `PREFIX`; from a laptop it's host mode.

## 4. Notes / limitations
- Promoted posts are filtered; reply/quote posts may still appear (easy to add).
- Some link/CTA text can leak into a post body — minor cleanup pending.
- `uiautomator` selectors can break when X updates its app; re-run after updates.
- **Comments:** `scrape_comments()` opens a tweet and reads replies via
  `uiautomator`. On the current X app, reply authors are generally **not exposed**
  in the accessibility hierarchy (the detail view returns only the original post,
  and can hang `uiautomator` dumps on video/complex threads). So it's **off by
  default**; set `XS_COMMENT_TOP=3` to attempt it — expect frequent empty results.
- For video output (future), the same `raw/` JSON feeds both articles and a
  later video stage.
