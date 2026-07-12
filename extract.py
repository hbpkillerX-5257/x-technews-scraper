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
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
RAW_DIR = PROJECT / "raw"
RAW_DIR.mkdir(exist_ok=True)

DEVICE = "100.91.248.110:35111"  # used only in host mode

# On-device Termux mode talks to the phone's OWN adbd over wireless debugging.
# Termux's app UID can't run input/uiautomator/monkey directly (no INJECT_EVENTS),
# so we route through `adb shell` to 127.0.0.1:<XS_ADB_PORT>.
LOCAL_ADB_PORT = os.environ.get("XS_ADB_PORT", "35111")
ADB_BIN = shutil.which("adb") or "adb"


# Auto-detect Termux; override with XS_DEVICE=1 / XS_DEVICE=0.
ON_DEVICE = os.environ.get("XS_DEVICE", "").lower() in ("1", "true") or (
    os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
)


def cmd(args):
    """Run a command via `adb shell` (host or on-device-self) or locally."""
    args = list(args)
    if ON_DEVICE and shutil.which("adb"):
        full = [ADB_BIN, "-s", f"127.0.0.1:{LOCAL_ADB_PORT}", "shell"] + args
    elif ON_DEVICE:
        # Fallback: direct exec (only works for non-input cmds; lacks perms).
        full = ["sh", "-c", " ".join(args)]
    else:
        full = ["adb", "-s", DEVICE, "shell"] + args
    r = subprocess.run(full, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[cmd err] {' '.join(args)} -> {r.stderr.strip()[:200]}")
    return r


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
    cmd(["uiautomator", "dump", "/sdcard/ui.xml"])
    if ON_DEVICE and shutil.which("adb"):
        # Stream the dump over adb (exec-out) to avoid Termux storage-permission
        # issues reading /sdcard directly.
        r = subprocess.run(
            [ADB_BIN, "-s", f"127.0.0.1:{LOCAL_ADB_PORT}", "exec-out",
             "cat", "/sdcard/ui.xml"],
            capture_output=True,
        )
        try:
            return ET.fromstring(r.stdout)
        except ET.ParseError:
            return None
    p = Path("/tmp/opencode/x_ui.xml")
    if not ON_DEVICE:
        subprocess.run(["adb", "-s", DEVICE, "pull", "/sdcard/ui.xml", str(p)],
                       capture_output=True)
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
    r"(\d+\s*(?:h|m|s|d|hour|minute|second|day)s?\s*ago|just now|yesterday)",
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


def extract_tweets(root):
    out = []
    for node in root.iter("node"):
        cd = node.get("content-desc", "")
        if not cd:
            continue
        t = parse_tweet(cd)
        if t:
            out.append(t)
    return out


def run(scrolls=8, tab="For you"):
    mode = f"ON-DEVICE (Termux -> adb 127.0.0.1:{LOCAL_ADB_PORT})" if ON_DEVICE else f"host via {DEVICE}"
    print(f"[mode: {mode}]")
    # Keep the display on and wake it so wm/uiautomator/input have a window.
    cmd(["input", "keyevent", "KEYCODE_WAKEUP"])
    cmd(["svc", "power", "stayon", "true"])
    w, h = wm_size()
    launch_x()
    if tab:
        tap_content_desc(tab)
        time.sleep(3)
    seen = {}
    for i in range(scrolls + 1):
        root = dump_ui()
        if root is not None:
            for t in extract_tweets(root):
                key = (t["handle"], t["body"])
                if key not in seen:
                    seen[key] = t
        print(f"scroll {i}: {len(seen)} unique tweets so far")
        if i < scrolls:
            scroll_up(w, h)
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
