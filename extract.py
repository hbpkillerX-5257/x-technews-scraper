#!/usr/bin/env python3
"""X (Twitter) feed extractor — runs on-device (Termux) or from a host via ADB.

On-device (Termux): commands are sent through the phone's OWN adbd via
`adb -s 127.0.0.1:<XS_ADB_PORT> shell` (Termux's app UID lacks the
INJECT_EVENTS permission needed for input/uiautomator/monkey). The UI dump is
read from /sdcard/ui.xml on the device.
Host mode: same commands over `adb -s DEVICE shell` from a PC.

Extracts structured tweets (author, handle, body, time, engagement),
skips ads, saves raw JSON. Stage 1 of the tech-news pipeline.
"""
import os
import re
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
RAW_DIR = PROJECT / "raw"
RAW_DIR.mkdir(exist_ok=True)

# On-device Termux mode talks to the phone's OWN adbd over wireless debugging.
# Termux's app UID can't run input/uiautomator/monkey directly (no INJECT_EVENTS),
# so we route through `adb shell` to 127.0.0.1:<XS_ADB_PORT>.
LOCAL_ADB_PORT = os.environ.get("XS_ADB_PORT", "35111")
ADB_BIN = shutil.which("adb") or "adb"
# Base IP used to recognise the phone when auto-discovering the device.
ADB_IP = os.environ.get("XS_ADB_IP", "100.91.248.110")
ADB_TARGET_FILE = PROJECT / ".adb_target"   # remembers last-known ip:port
ACTIVE_DEVICE = None


# Auto-detect Termux; override with XS_DEVICE=1 / XS_DEVICE=0.
ON_DEVICE = os.environ.get("XS_DEVICE", "").lower() in ("1", "true") or (
    os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
)

# How many top-by-engagement tweets to open and scrape replies for (0 = off).
# How many top tweets (by visible position) to attempt comment scraping on.
# Default 0: the current X app rarely exposes reply authors via uiautomator,
# and the detail view can hang dumps, so leave it off unless you want to try.
COMMENT_TOP_N = int(os.environ.get("XS_COMMENT_TOP", "0"))


def _save_target(t):
    try:
        ADB_TARGET_FILE.write_text(t)
    except OSError:
        pass


def _load_target():
    try:
        return ADB_TARGET_FILE.read_text().strip()
    except OSError:
        return ""


def discover_adb_mdns(timeout=8):
    """Find the phone's current wireless-debugging ip:port via mDNS.

    Android advertises its wireless-debugging service over mDNS, so the port
    (which randomises every session) is discoverable automatically.
    Requires `zeroconf` (pip install zeroconf); returns None if unavailable.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except Exception:  # noqa
        return None
    found = {}

    class _Listener:
        def add_service(self, zc, t, name):
            info = zc.get_service_info(t, name)
            if info and info.port:
                for a in info.addresses:
                    found[f"{socket.inet_ntoa(a)}:{info.port}"] = True

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, "_adb-tls-connect._tcp.local.", _Listener())
        for _ in range(timeout * 10):
            if found:
                break
            time.sleep(0.1)
    except Exception:  # noqa
        return None
    finally:
        try:
            if zc:
                zc.close()
        except Exception:
            pass
    return next(iter(found), None)


def resolve_device():
    """Auto-detect the connected adb device; prefer ADB_IP; else last-known."""
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = re.search(r"^(\S+)\s+device$", line)
        if m:
            found.append(m.group(1))
    if not found:
        md = discover_adb_mdns()
        if md:
            _save_target(md)
            return md
        return _load_target() or os.environ.get("XS_DEVICE") or None
    for c in found:
        if c.startswith(ADB_IP):
            _save_target(c)
            return c
    _save_target(found[0])
    return found[0]


def get_device():
    global ACTIVE_DEVICE
    if ON_DEVICE:
        return None
    if ACTIVE_DEVICE:
        return ACTIVE_DEVICE
    ACTIVE_DEVICE = resolve_device()
    return ACTIVE_DEVICE


def cmd(args, timeout=None):
    """Run a command via `adb shell` (host or on-device-self) or locally."""
    args = list(args)
    if ON_DEVICE and shutil.which("adb"):
        full = [ADB_BIN, "-s", f"127.0.0.1:{LOCAL_ADB_PORT}", "shell"] + args
    elif ON_DEVICE:
        # Fallback: direct exec (only works for non-input cmds; lacks perms).
        full = ["sh", "-c", " ".join(args)]
    else:
        full = ["adb", "-s", get_device(), "shell"] + args
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[cmd timeout] {' '.join(args)}")
        return subprocess.CompletedProcess(full, -1, "", "")
    if r.returncode != 0:
        print(f"[cmd err] {' '.join(args)} -> {r.stderr.strip()[:200]}")
    return r


def adb_connected():
    t = get_device()
    if not t:
        return False
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    return any(t in line and "device" in line for line in out.splitlines())


def ensure_connected():
    """Host mode: reconnect if adb dropped (wireless debugging is flaky).

    Tries, in order: the resolved/cached target, an mDNS-rediscovered port,
    and the common pinned tcpip port (after `adb tcpip 5555`).
    """
    global ACTIVE_DEVICE
    if ON_DEVICE:
        return True
    if adb_connected():
        return True
    cands = []
    t = get_device() or _load_target() or os.environ.get("XS_DEVICE")
    if t:
        cands.append(t)
    md = discover_adb_mdns()
    if md:
        cands.append(md)
    cands.append(f"{ADB_IP}:5555")  # pinned tcpip port
    for c in cands:
        print(f"[adb] trying {c} ...")
        subprocess.run(["adb", "connect", c], capture_output=True, text=True)
        time.sleep(2)
        ACTIVE_DEVICE = None  # re-resolve after connect
        if adb_connected():
            _save_target(c)
            return True
    print("[adb] not connected. Run: adb connect <phone-ip>:<port> "
          "(port from phone's Wireless debugging settings), then retry.")
    return False


def wm_size():
    out = cmd(["wm", "size"]).stdout
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def launch_x():
    cmd(["monkey", "-p", "com.twitter.android",
         "-c", "android.intent.category.LAUNCHER", "1"])
    time.sleep(5)


def tap_content_desc(target, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root = dump_ui()
        if root is None:
            time.sleep(1)
            continue
        for node in root.iter("node"):
            cd = node.get("content-desc", "")
            tx = node.get("text", "")
            if cd == target or tx == target:
                b = node.get("bounds", "")
                m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    x = (int(m.group(1)) + int(m.group(3))) // 2
                    y = (int(m.group(2)) + int(m.group(4))) // 2
                    cmd(["input", "tap", str(x), str(y)])
                    return True
        time.sleep(0.7)
    return False


def dump_ui():
    # uiautomator dump can hang on the X detail view (video/complex tweets);
    # the timeout keeps the pipeline from freezing. A None return means
    # "skip this scrape step" rather than aborting the whole run.
    cmd(["uiautomator", "dump", "/sdcard/ui.xml"], timeout=25)
    if ON_DEVICE and shutil.which("adb"):
        # Stream the dump over adb (exec-out) to avoid Termux storage-permission
        # issues reading /sdcard directly.
        r = subprocess.run(
            [ADB_BIN, "-s", f"127.0.0.1:{LOCAL_ADB_PORT}", "exec-out",
             "cat", "/sdcard/ui.xml"],
            capture_output=True,
            timeout=25,
        )
        try:
            return ET.fromstring(r.stdout)
        except ET.ParseError:
            return None
    p = Path("/tmp/opencode/x_ui.xml")
    if not ON_DEVICE:
        subprocess.run(["adb", "-s", get_device(), "pull", "/sdcard/ui.xml", str(p)],
                       capture_output=True, timeout=25)
    try:
        return ET.parse(p).getroot()
    except ET.ParseError:
        return None


def scroll_up(w, h, frac=0.75):
    x = w // 2
    y1 = int(h * frac)
    y2 = int(h * 0.15)
    cmd(["input", "swipe", str(x), str(y1), str(x), str(y2), "350"])
    time.sleep(2)


def refresh_feed(w, h):
    """Pull-to-refresh at the top of the feed (drag downward)."""
    x = w // 2
    y1 = int(h * 0.12)
    y2 = int(h * 0.45)
    cmd(["input", "swipe", str(x), str(y1), str(x), str(y2), "600"])
    time.sleep(1)


def _overlap(a, b):
    return (a or "")[:40] in (b or "") or (b or "")[:40] in (a or "")


def _eng_score(t):
    return sum((t.get("engagement", {}) or {}).values())


def tap_bounds(bounds):
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return False
    x = (int(m.group(1)) + int(m.group(3))) // 2
    y = (int(m.group(2)) + int(m.group(4))) // 2
    cmd(["input", "tap", str(x), str(y)])
    return True


def scroll_down(w, h, frac=0.25):
    """Swipe down (reveal content toward the top of the list)."""
    x = w // 2
    y1 = int(h * frac)
    y2 = int(h * 0.7)
    cmd(["input", "swipe", str(x), str(y1), str(x), str(y2), "350"])
    time.sleep(2)


def goto_top(w, h):
    for _ in range(12):
        scroll_down(w, h)


def scrape_comments(bounds, w, h, orig=None, scrolls=4):
    """Open a tweet (by its card bounds) and collect replies.

    NOTE: On the current X app, reply authors are generally NOT exposed in
    the accessibility hierarchy (uiautomator dump returns only the original
    post, or hangs on video/complex threads). So this frequently returns [].
    It is best-effort and wrapped so a failure never aborts the run.
    """
    try:
        if not tap_bounds(bounds):
            return []
        time.sleep(3)
        seen = {}
        for _ in range(scrolls):
            root = dump_ui()
            if root is not None:
                for c in extract_tweets(root):
                    if orig and c["handle"] == orig["handle"] and _overlap(c["body"], orig["body"]):
                        continue  # skip the original post itself
                    if AD_RE.search(c.get("raw", "")) or AD_RE.search(c["body"]):
                        continue
                    key = (c["handle"], c["body"])
                    if key not in seen:
                        seen[key] = c
            scroll_up(w, h)
        cmd(["input", "keyevent", "4"])  # back to feed
        time.sleep(2)
        return list(seen.values())
    except Exception as e:
        print(f"[comments] skipped: {e}")
        try:
            cmd(["input", "keyevent", "4"])
        except Exception:
            pass
        return []


AD_RE = re.compile(r"\b(promoted|advertisement|ad\s*·|sponsored)\b", re.IGNORECASE)
META_RE = re.compile(
    r"\s+(\d+\s*(?:second|minute|hour|day|s|m|h|d)?\s*ago"
    r"[.\s]*(?:\d+\s*replies?)?"
    r"[.\s]*(?:\d+\s*reposts?)?"
    r"[.\s]*(?:\d+\s*likes?)?"
    r"[.\s]*(?:\d[\d,]*\s*verified views?)?\.?)\s*$",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(\d+\s*(?:h|m|s|d|hr|hrs|hour|minute|second|day)s?\b(?:\s*ago)?"
    r"|just now|yesterday)",
    re.IGNORECASE,
)


def parse_tweet(cd):
    if AD_RE.search(cd):
        return None  # skip ads / promoted posts
    hm = re.search(r"@(\w+)", cd)
    if not hm:
        return None
    if not TIME_RE.search(cd):
        return None
    name = cd[:hm.start()].strip()
    rest = cd[hm.end():].strip()
    rest = re.sub(r"^Verified\.?\s*", "", rest, flags=re.IGNORECASE)
    rest = META_RE.sub("", rest).strip()
    body = re.sub(r"\s*\n\s*", "\n", rest).strip()
    if not body:
        return None
    eng = {}
    for label in ("replies", "reposts", "likes"):
        m = re.search(rf"(\d[\d,]*)\s*{label}", cd, re.IGNORECASE)
        if m:
            eng[label] = int(m.group(1).replace(",", ""))
    vm = re.search(r"(\d[\d,]*)\s*verified views", cd, re.IGNORECASE)
    if vm:
        eng["views"] = int(vm.group(1).replace(",", ""))
    tm = TIME_RE.search(cd)
    return {
        "name": name,
        "handle": hm.group(1),
        "body": body,
        "time": tm.group(1) if tm else None,
        "engagement": eng,
        "raw": cd,
    }


def _node_texts(node):
    return [c.get("text", "").strip() for c in node.iter("node")
            if c.get("text", "").strip()]


def _build_from_texts(texts, handle, name):
    body = max(
        (t for t in texts
         if t != handle and not re.match(r"^[\d.,KkMm]+$", t)
         and not TIME_RE.search(t)),
        key=len, default="",
    )
    tm = next((t for t in texts if TIME_RE.search(t)), None)
    return {
        "name": name or "",
        "handle": handle.lstrip("@"),
        "body": body,
        "time": tm,
        "engagement": {},
        "raw": " | ".join(texts[:6]),
    }


def extract_tweets(root):
    """Extract tweets from either representation:
    - legacy: full post in a node's `content-desc`
    - current: post split across `text` nodes of a tweet card
    """
    parent = {c: p for p in root.iter() for c in p}
    out = []
    for n in root.iter("node"):
        cd = n.get("content-desc", "").strip()
        if cd:
            t = parse_tweet(cd)
            if t:
                t["bounds"] = n.get("bounds", "")
                out.append(t)
            continue
        t = n.get("text", "").strip()
        if not re.match(r"^@\w+$", t):
            continue
        # Climb to the tweet card (ancestor holding both body and time).
        card = n
        last = n
        while card is not None:
            texts = _node_texts(card)
            has_body = any(len(x) > 40 for x in texts)
            has_time = any(TIME_RE.search(x) for x in texts)
            if has_body and has_time:
                break
            if has_body:
                last = card
            card = parent.get(card)
        if card is None:
            card = last
        texts = _node_texts(card)
        if any(AD_RE.search(x) for x in texts):
            continue  # skip promoted / ads
        # Guess display name: last plausible short text before the handle.
        name = ""
        for c in card.iter("node"):
            ct = c.get("text", "").strip()
            if ct == t:
                break
            if (ct and not re.match(r"^@\w+$", ct) and not TIME_RE.search(ct)
                    and not re.match(r"^[\d.,KkMm]+$", ct) and len(ct) <= 40):
                name = ct
        rec = _build_from_texts(texts, t, name)
        if rec["body"]:
            rec["bounds"] = card.get("bounds", "")
            out.append(rec)
    return out


def run(scrolls=8, tab="For you"):
    if not ON_DEVICE and not ensure_connected():
        raise SystemExit("Cannot reach device. Check wireless debugging / USB.")
    mode = f"ON-DEVICE (Termux -> adb 127.0.0.1:{LOCAL_ADB_PORT})" if ON_DEVICE else f"host via {get_device()}"
    print(f"[mode: {mode}]")
    # Keep the display on and wake it so wm/uiautomator/input have a window.
    cmd(["input", "keyevent", "KEYCODE_WAKEUP"])
    cmd(["svc", "power", "stayon", "true"])
    w, h = wm_size()
    launch_x()
    if tab:
        tap_content_desc(tab)
        time.sleep(3)
    # Refresh the feed, then wait for it to load before scraping.
    refresh_feed(w, h)
    print("feed refreshed, waiting 60s for load...")
    time.sleep(60)
    seen = {}
    for i in range(scrolls + 1):
        if not ON_DEVICE:
            ensure_connected()
        root = dump_ui()
        if root is not None:
            for t in extract_tweets(root):
                key = (t["handle"], t["body"])
                if key not in seen:
                    seen[key] = t
        print(f"scroll {i}: {len(seen)} unique tweets so far")
        if i < scrolls:
            scroll_up(w, h)
    # Scrape replies for the top-N tweets currently visible at the feed top
    # (their captured bounds are valid there).
    if COMMENT_TOP_N > 0:
        goto_top(w, h)
        root = dump_ui()
        visible = extract_tweets(root) if root is not None else []
        for v in sorted(visible, key=_eng_score, reverse=True)[:COMMENT_TOP_N]:
            if not v.get("bounds"):
                continue
            print(f"scraping comments for @{v['handle']} ...")
            v["comments"] = scrape_comments(v["bounds"], w, h, orig=v)

    tweets = list(seen.values())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RAW_DIR / f"tweets_{stamp}.json"
    out_path.write_text(__import__("json").dumps(tweets, indent=2, ensure_ascii=False))
    print(f"saved {len(tweets)} tweets -> {out_path}")
    return out_path


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    run(scrolls=n)
