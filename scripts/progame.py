#!/usr/bin/env python3
"""Nintendo Switch game listings from ProGame."""
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(WEBAPP_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
import db  # noqa: E402
from _scraper_utils import Node, TreeParser, clean, game_title, get_image_url  # noqa: E402

BASE = "https://progame.ir"
URL = f"{BASE}/product-category/games/game-nintendo-switch/"
SOURCE = "progame_nintendo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def fetch(url):
    parts = urlsplit(url)
    encoded = urlunsplit((parts.scheme, parts.netloc,
                          quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                          parts.query, parts.fragment))
    request = Request(encoded, headers={"User-Agent": USER_AGENT,
                                        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=40) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def descendant(node, tag=None, class_name=None):
    found = []
    for child in node.children:
        if isinstance(child, Node):
            if (tag is None or child.tag == tag) and (class_name is None or child.has_class(class_name)):
                found.append(child)
            found.extend(descendant(child, tag, class_name))
    return found


def price_digits(value):
    value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    match = re.search(r"\d[\d٬,\.\s]*", value)
    return re.sub(r"\D", "", match.group(0)) if match else ""


def parse_page(html):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for card in parser.root.find("products"):
        for item in descendant(card, "li", "product"):
            headings = descendant(item, "h2", "woocommerce-loop-product__title")
            raw_title = clean(headings[0].text()) if headings else ""
            links = descendant(item, "a", "woocommerce-LoopProduct-link")
            href = links[0].attrs.get("href", "") if links else ""
            if not raw_title or not href:
                continue
            # Drop the Persian catalog prefix while retaining the actual game name.
            title = re.sub(r"^(?:خرید\s+)?(?:بازی\s+)?", "", raw_title, flags=re.I)
            title = game_title(title)
            title = re.sub(r"\s*[|｜].*$", "", title)
            title = re.sub(r"\s+(?:برای\s+)?(?:نینتندو\s*)?(?:Nintendo\s*)?(?:Switch|سوییچ|سویچ)\s*[۱۲12]?\s*$", "", title, flags=re.I).strip()
            title = re.sub(r"\s+برای\s*$", "", title, flags=re.I).strip()
            if not title:
                continue
            prices = descendant(item, "span", "woocommerce-Price-amount")
            price_raw = clean(prices[0].text()) if prices else ""
            digits = price_digits(price_raw)
            amount = int(digits) if digits else 0
            classes = item.attrs.get("class", "").split()
            in_stock = amount > 0 and "outofstock" not in classes
            searchable = f"{raw_title} {href}".casefold()
            platform = "Switch2" if re.search(r"switch\s*[- ]?2|سوییچ\s*۲|سوییچ\s*2", searchable) else "Switch1"
            products.append({
                "title": title,
                "text": f"قیمت: {amount:,} تومان" if amount else "قیمت: 0 تومان",
                "link": urljoin(BASE, href),
                "condition": "new",
                "in_stock": in_stock,
                "platform": platform,
                "image_url": get_image_url(item, BASE),
            })

    for anchor in descendant(parser.root, "a"):
        href = anchor.attrs.get("href", "")
        parsed = urlparse(urljoin(BASE, href))
        page = parse_qs(parsed.query).get("product-page") or parse_qs(parsed.query).get("paged")
        if parsed.path.rstrip("/") == urlparse(URL).path.rstrip("/") and page:
            pages.add(parsed.geturl())
    return products, pages


def scrape():
    queue, visited, found = [URL], set(), {}
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        products, pages = parse_page(fetch(current))
        for item in products:
            found[item["link"]] = item
        queue.extend(page for page in pages if page not in visited)
    return list(found.values())


def main():
    db.init_db()
    products = scrape()
    if not products:
        raise SystemExit(f"[{SOURCE}] No games collected; page structure may have changed.")
    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": SOURCE, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    print(f"[{SOURCE}] {len(products)} games scanned; {sum(p['in_stock'] for p in products)} available, {added} new, {updated} updated.")


if __name__ == "__main__":
    main()
