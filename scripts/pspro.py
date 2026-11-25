#!/usr/bin/env python3
"""Nintendo Switch 1/2 new and used games from PSPro."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
import db  # noqa: E402
from _scraper_utils import Node, TreeParser, clean, game_title, get_image_url  # noqa: E402

BASE = "https://pspro.ir"
SOURCE = "pspro_nintendo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CATEGORIES = (
    ("https://pspro.ir/category/nintendo-switch-2-games", "Switch2", "new"),
    ("https://pspro.ir/category/بازی-کارکرده-نینتندو-سوییچ", "Switch1", "used"),
    ("https://pspro.ir/index.php?route=product/category&path=105_236_409", "Switch2", "used"),
)


def fetch(url):
    parts = urlsplit(url)
    safe_url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"), parts.query, parts.fragment))
    request = Request(safe_url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=15) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def descendants(node, tag=None, class_name=None):
    out = []
    for child in node.children:
        if isinstance(child, Node):
            if (tag is None or child.tag == tag) and (class_name is None or child.has_class(class_name)):
                out.append(child)
            out.extend(descendants(child, tag, class_name))
    return out


def parse_page(html, platform, condition, category_url):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    cards = [n for n in descendants(parser.root, "div", "product")]
    for card in cards:
        anchors = descendants(card, "a")
        product_link = next((a.attrs.get("href", "") for a in anchors
                             if "/product/" in a.attrs.get("href", "")), "")
        if not product_link:
            continue
        searchable = " ".join([product_link, card.text(), *(a.attrs.get("title", "") for a in anchors)])
        if not re.search(r"Nintendo|Switch|نینتندو|سوییچ", searchable, re.I):
            continue
        image_url = get_image_url(card, BASE)
        raw_title = ""
        for node in descendants(card, "h6"):
            value = clean(node.text())
            if value and not re.search(r"نینتندو\s*سوییچ|بازی نینتندو", value, re.I):
                raw_title = value
                break
        if not raw_title:
            raw_title = next((clean(a.attrs.get("title", "")) for a in anchors if a.attrs.get("title")), "")
        raw_title = re.sub(r"^بازی\s+", "", raw_title, flags=re.I)
        raw_title = re.sub(r"\s*(?:[-–—|]\s*)?(?:کارکرده|دست\s*دوم|استوک|used|second[ -]?hand)\s*$", "", raw_title, flags=re.I)
        title = game_title(raw_title)
        title = re.sub(r"\s+(?:برای\s+)?(?:Nintendo\s*)?Switch\s*[12]?\s*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s*(?:[-–—]\s*)?(?:Nintendo\s*)?Switch\s*[12]?\s*$", "", title, flags=re.I).strip()
        if not title:
            continue
        buttons = descendants(card, "button")
        price_text = clean(buttons[0].text()) if buttons else ""
        price_text = price_text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        match = re.search(r"\d[\d٬,،.\s]*", price_text)
        digits = re.sub(r"\D", "", match.group(0)) if match else ""
        amount = int(digits) if digits else 0
        unavailable = bool(re.search(r"ناموجود|اتمام\s*موجودی|موجود\s*نیست", card.text(), re.I))
        products.append({
            "title": title,
            "text": f"قیمت: {amount:,} تومان" if amount else "قیمت: 0 تومان",
            "link": urljoin(BASE, product_link),
            "condition": condition,
            "in_stock": amount > 0 and bool(buttons) and not unavailable,
            "platform": platform,
            "image_url": image_url,
        })

    for anchor in descendants(parser.root, "a"):
        href = anchor.attrs.get("href", "")
        absolute = urljoin(BASE, href)
        parsed, category = urlparse(absolute), urlparse(category_url)
        parsed_q, category_q = parse_qs(parsed.query), parse_qs(category.query)
        same_route = parsed.path == category.path and parsed_q.get("route", [None])[0] == category_q.get("route", [None])[0] and parsed_q.get("path", [None])[0] == category_q.get("path", [None])[0]
        if same_route and "page" in parsed_q:
            pages.add(absolute)
    return products, pages


def scrape_category(url, platform, condition):
    pending, visited, found = {url}, set(), {}
    # Process a pagination frontier concurrently. The old breadth-first loop
    # fetched every page serially, so three large categories were vulnerable
    # to the scheduler's 120-second limit.
    with ThreadPoolExecutor(max_workers=6) as pool:
        while pending:
            batch = sorted(pending - visited)
            pending.clear()
            if not batch:
                break
            visited.update(batch)
            futures = [pool.submit(fetch, page) for page in batch]
            for page, future in zip(batch, futures):
                products, pages = parse_page(future.result(), platform, condition, url)
                for item in products:
                    found[item["link"]] = item
                pending.update(pages - visited)
    return list(found.values())


def main():
    db.init_db()
    products, errors = [], []
    for url, platform, condition in CATEGORIES:
        try:
            page_products = scrape_category(url, platform, condition)
            products.extend(page_products)
            print(f"[{SOURCE}] {platform} {condition}: {len(page_products)} scanned.")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if not products:
        raise SystemExit(f"[{SOURCE}] No games collected.\n" + "\n".join(errors))
    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": SOURCE, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    print(f"[{SOURCE}] {len(products)} games scanned; {sum(x['in_stock'] for x in products)} available, {added} new, {updated} updated.")
    for error in errors:
        print(f"[{SOURCE}] {error}")


if __name__ == "__main__":
    main()
