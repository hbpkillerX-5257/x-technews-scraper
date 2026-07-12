#!/usr/bin/env python3
"""Stage 2: rewrite scraped X tweets into tech-news articles via LLM.

OpenAI-compatible chat endpoint (bynara router) + mistral-large.
Exponential backoff on errors. Output: Markdown articles + index.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parent
RAW_DIR = PROJECT / "raw"
ART_DIR = PROJECT / "articles"
ART_DIR.mkdir(exist_ok=True)


def _load_dotenv(path=PROJECT / ".env"):
    """Minimal .env loader (no external deps)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

BASE_URL = os.environ.get("XS_BASE_URL", "https://router.bynara.id/v1")
MODEL = os.environ.get("XS_MODEL", "mistral-large")
API_KEY = os.environ.get("XS_API_KEY")
if not API_KEY:
    raise SystemExit("XS_API_KEY not set. Put it in .env or export it.")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def chat(messages, temperature=0.6, retries=6):
    payload = {"model": MODEL, "messages": messages, "temperature": temperature}
    delay = 1.0
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{BASE_URL}/chat/completions",
                headers=HEADERS,
                json=payload,
                timeout=120,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            print(f"  http {r.status_code}: {r.text[:200]} -- retry {attempt+1}")
        except Exception as e:  # noqa
            print(f"  error: {e} -- retry {attempt+1}")
        time.sleep(delay + (attempt * 0.3))
        delay *= 2
    raise RuntimeError("LLM calls exhausted retries")


def load_latest_raw():
    files = sorted(RAW_DIR.glob("tweets_*.json"))
    if not files:
        raise SystemExit("no raw tweets found in raw/")
    return files[-1]


def build_prompt(tweets):
    lines = []
    for i, t in enumerate(tweets):
        eng = t.get("engagement", {})
        eng_s = ", ".join(f"{k}={v}" for k, v in eng.items()) or "n/a"
        blk = f"[{i}] @{t['handle']} ({t.get('name','')}) | {t.get('time','')} | {eng_s}\n{t['body']}"
        comments = t.get("comments") or []
        if comments:
            blk += "\nTop comments:\n" + "\n".join(
                f"  - @{c['handle']}: {c['body'][:160]}" for c in comments[:3]
            )
        lines.append(blk)
    feed = "\n\n".join(lines)
    system = (
        "You are a tech-news editor. You receive raw posts scraped from an "
        "X/Twitter 'Following' feed of a tech-focused reader. Your job:\n"
        "1. Keep ONLY posts relevant to technology, AI, software, hardware, "
        "startups, science, or developer tools. DROP personal, promotional, "
        "or non-tech posts unless they announce genuine tech news.\n"
        "2. Group related posts into coherent stories.\n"
        "3. Write clean, factual tech-news articles (markdown body). Attribute "
        "sources by @handle at the end.\n"
        "4. You may reference notable 'Top comments' to add context, but keep "
        "the article factual and attribute opinions to the commenter.\n"
        "Output STRICTLY a JSON array (no prose, no code fences). Each item:\n"
        "{\"headline\": str, \"category\": str, \"summary\": str, "
        "\"body\": str (markdown, 2-4 short paragraphs), "
        "\"sources\": [\"@handle\", ...]}\n"
        "If nothing is tech-relevant, return []."
    )
    user = f"RAW FEED:\n\n{feed}\n\nReturn the JSON array of articles."
    return system, user


def parse_json_array(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "article"


def main():
    raw_path = Path(sys.argv[1]) if len(sys.argv) > 1 else load_latest_raw()
    tweets = json.loads(Path(raw_path).read_text())
    print(f"loaded {len(tweets)} tweets from {raw_path}")

    system, user = build_prompt(tweets)
    print("calling LLM (with backoff)...")
    out = chat([{"role": "system", "content": system},
                {"role": "user", "content": user}])
    articles = parse_json_array(out)
    print(f"LLM produced {len(articles)} article(s)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    index = []
    for a in articles:
        slug = slugify(a.get("headline", ""))
        fm = (
            "---\n"
            f"title: {a.get('headline','')}\n"
            f"category: {a.get('category','')}\n"
            f"date: {stamp}\n"
            f"sources: {', '.join(a.get('sources', []))}\n"
            "---\n\n"
            f"# {a.get('headline','')}\n\n"
            f"_{a.get('summary','')}_\n\n"
            f"{a.get('body','')}\n\n"
            "**Sources:** " + ", ".join(a.get("sources", [])) + "\n"
        )
        p = ART_DIR / f"{stamp}_{slug}.md"
        p.write_text(fm, encoding="utf-8")
        index.append((a.get("headline", ""), p.name, a.get("category", "")))
        print(f"  wrote {p.name}")

    idx = ["# Tech News Digest", "", f"_Generated {stamp}_", ""]
    for title, fname, cat in index:
        idx.append(f"- [{title}]({fname}) — {cat}")
    (ART_DIR / "index.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    print(f"wrote index.md ({len(index)} articles)")


if __name__ == "__main__":
    main()
