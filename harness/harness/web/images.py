"""Shoe images: for each candidate, Nimble finds pages about the shoe and we read the page's og:image.

Prefetch only (scripts/add_images.py). Product pages are tried first, then reviews; an image counts
only if its URL actually serves an image. Shoes without one keep imageUrl = None."""
from __future__ import annotations

import concurrent.futures as cf
import re

import httpx

from harness.web.research import nimble_search

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
GENERIC = re.compile(r"og-image|default|social|logo|thumbnail|placeholder|banner|share|attends|blog", re.I)
OG = [re.compile(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I),
      re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', re.I)]


def og_image(url: str) -> str | None:
    try:
        r = httpx.get(url, headers=UA, timeout=8, follow_redirects=True)
        if r.status_code != 200:
            return None
        for rx in OG:
            m = rx.search(r.text[:300_000])
            if m:
                return httpx.URL(url).join(m.group(1).replace("&amp;", "&")).__str__()
    except Exception:
        return None
    return None


def serves_image(url: str) -> bool:
    try:
        r = httpx.get(url, headers=UA, timeout=8, follow_redirects=True)
        return r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) > 5_000
    except Exception:
        return False


def find_image(name: str) -> dict | None:
    pages = []
    for q in (f"{name} running shoe", f"{name} review"):
        try:
            pages += nimble_search(q, 6)
        except Exception:
            pass
    pages.sort(key=lambda p: 0 if re.search(r"/products?/|descpage|/p/", p.get("url", "")) else 1)
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2]
    model = words[-2:] if len(words) >= 2 else words
    for p in pages:
        img = og_image(p.get("url", ""))
        if not img or GENERIC.search(img):
            continue
        product_page = bool(re.search(r"/products?/|descpage|/p/", p["url"])) and any(w in p["url"].lower() for w in model)
        if (any(w in img.lower() for w in model) or product_page) and serves_image(img):
            return {"imageUrl": img, "imagePage": p["url"]}
    return None


def add_images(candidates: list[dict]) -> int:
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(lambda c: find_image(c["name"]), candidates))
    for c, f in zip(candidates, found):
        c["imageUrl"] = f["imageUrl"] if f else None
        c["imagePage"] = f["imagePage"] if f else None
    return sum(1 for f in found if f)
