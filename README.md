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

---

## 1. Requirements
- Android phone with the **X app installed and logged in**.
- **Termux** (F-Droid) with SSH, or any machine with `adb` + Python 3.
- The phone must allow UI automation (`input` / `uiautomator`). On a Pixel this
  works from Termux directly once the app is open.

## 2. Setup (Termux — recommended, 24/7)

Termux's app sandbox **cannot** run `input` / `uiautomator` / `monkey` directly
(no `INJECT_EVENTS` permission). The fix: install `adb` in Termux and drive the
phone's *own* wireless-debugging adbd (`127.0.0.1:<port>`), which runs as the
privileged `shell` user.

```bash
pkg update && pkg install python git android-tools
pip install requests
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

### Host mode (run from a PC instead)
If you prefer running from a laptop, keep the phone connected over wireless
debugging and set the device serial in `extract.py` (`DEVICE = "ip:port"`).
Force host mode with:
```bash
XS_DEVICE=0 python3 extract.py 8
```
On-device mode is auto-detected via Termux's `PREFIX` env var, and uses the
local `adb` binary against `127.0.0.1:$XS_ADB_PORT`.

## 3. Configuration
Secrets live in a `.env` file (gitignored). Copy the template and fill in:
```bash
cp .env.example .env
# edit .env: set XS_API_KEY, optionally XS_BASE_URL / XS_MODEL
```
`rewrite.py` reads these via env (`XS_API_KEY`, `XS_BASE_URL`, `XS_MODEL`).
You may also export them in the shell instead of using `.env`.

`extract.py` settings:
```python
DEVICE = "100.91.248.110:35111"  # host mode only
# ON_DEVICE auto-detects Termux; override with XS_DEVICE=1 / XS_DEVICE=0
```

## 4. Notes / limitations
- Promoted posts are filtered; reply/quote posts may still appear (easy to add).
- Some link/CTA text can leak into a post body — minor cleanup pending.
- `uiautomator` selectors can break when X updates its app; re-run after updates.
- For video output (future), the same `raw/` JSON feeds both articles and a
  later video stage.
