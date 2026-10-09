"""Tests every URL in feeds.txt from the machine it runs on (GitHub)."""
import requests

from post_news import HEADERS, is_football, load_feeds, parse_feed

for url in load_feeds():
    print("=" * 70)
    print(url)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except Exception as e:
        print("  RESULT: NOT REACHABLE:", e)
        continue
    print(f"  HTTP {r.status_code}, {len(r.content)} bytes, {r.headers.get('content-type', '?')}")
    if r.status_code != 200:
        print("  RESULT: BLOCKED OR NOT FOUND" + (" (403 = site blocks GitHub)" if r.status_code == 403 else ""))
        continue
    try:
        items = parse_feed(r.content)
    except Exception as e:
        print("  RESULT: NOT A VALID RSS/ATOM FEED:", e)
        continue
    football = [i for i in items if is_football(i)]
    with_img = [i for i in items if i["image"]]
    print(f"  RESULT: WORKS - {len(items)} items, {len(football)} football, {len(with_img)} with image")
    for i in items[:3]:
        print("   -", i["title"][:90])
