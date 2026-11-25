#!/usr/bin/env python3
"""Nintendo Switch 1/2 new and used games from RoseGamers."""
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

BASE = "https://rosegamers.com"
SOURCE = "rosegamers_nintendo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
TIMEOUT = 45
CATEGORIES = (
    (f"{BASE}/game/nintendo-games/nintendo-switch-2-games", "Switch2", "new"),
    (f"{BASE}/game/nintendo-games/nintendo-switch-games", "Switch1", "new"),
    (f"{BASE}/game/nintendo-games/switch-used-games", "", "used"),
)


def fetch(url):
    parts = urlsplit(url)
    encoded = urlunsplit((parts.scheme, parts.netloc,
                          quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                          parts.query, parts.fragment))
    request = Request(encoded, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    })
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def first_text(node, class_name):
    nodes = node.find(class_name)
    return clean(nodes[0].text()) if nodes else ""


def first_attr(node, tag, attr):
    if node.tag == tag and node.attrs.get(attr):
        return node.attrs[attr]
    for child in node.children:
        if isinstance(child, Node):
            value = first_attr(child, tag, attr)
            if value:
                return value
    return ""


def price_digits(value):
    # Normalize Persian/Arabic digits before parsing the displayed toman price.
    value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    match = re.search(r"\d[\d٬,\.\s]*", value)
    return re.sub(r"\D", "", match.group(0)) if match else ""


def parse_page(html, url, expected_platform, expected_condition):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for card in parser.root.find("product-layout"):
        title_link = next((n for n in card.find("name") for _ in [0]), None)
        raw_title = clean(title_link.text()) if title_link else clean(first_attr(card, "img", "alt"))
        link = ""
        for anchor in card.find("product-img"):
            if anchor.tag == "a" and anchor.attrs.get("href"):
                link = anchor.attrs["href"]
                break
        if not link:
            link = first_attr(card, "a", "href")
        if not raw_title or not link:
            continue

        searchable = f"{raw_title} {first_text(card, 'stat-2')} {link} {card.text()}".casefold()
        if not re.search(r"switch|نینتندو|سوییچ", searchable):
            continue
        platform = expected_platform
        if re.search(r"switch\s*[- ]?2|سوییچ\s*۲|سوییچ\s*2|نینتندو\s*۲", searchable):
            platform = "Switch2"
        elif not platform and re.search(r"switch|نینتندو|سوییچ", searchable):
            platform = "Switch1"

        is_used = expected_condition == "used" or bool(re.search(r"کارکرده|دست\s*دوم|استوک|used", searchable))
        condition = "used" if is_used else "new"

        prices = card.find("price-new") or card.find("price-normal")
        price_text = clean(prices[0].text()) if prices else ""
        digits = price_digits(price_text)
        amount = int(digits) if digits else 0
        labels = " ".join(n.text() for n in card.find("product-label"))
        class_text = " ".join(n.attrs.get("class", "") for n in [card, *card.find("product-thumb")])
        availability_text = f"{labels} {card.text()}".casefold()
        unavailable = bool(re.search(r"ناموجود|اتمام\s*موجودی|out.of.stock", availability_text))
        has_cart = bool(card.find("btn-cart"))
        in_stock = amount > 0 and not unavailable and (has_cart or "out-of-stock" not in class_text)

        products.append({
            "title": game_title(raw_title),
            "text": f"قیمت: {amount:,} تومان" if amount else "قیمت: 0 تومان",
            "link": urljoin(BASE, link),
            "condition": condition,
            "in_stock": in_stock,
            "platform": platform,
            "image_url": get_image_url(card, BASE),
        })

    def walk(node):
        if node.tag == "a":
            href = node.attrs.get("href", "")
            absolute = urljoin(BASE, href)
            if urlparse(absolute).path.rstrip("/") == urlparse(url).path.rstrip("/"):
                query = parse_qs(urlparse(absolute).query)
                if query.get("page"):
                    pages.add(absolute)
        for child in node.children:
            if isinstance(child, Node):
                walk(child)
    walk(parser.root)
    return products, pages


def scrape_category(url, platform, condition):
    queue, visited, found = [url], set(), {}
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        products, pages = parse_page(fetch(current), url, platform, condition)
        for product in products:
            key = product["link"] or product["title"]
            found[key] = product
        queue.extend(page for page in pages if page not in visited)
    return list(found.values())


def main():
    db.init_db()
    all_products, errors = [], []
    for url, platform, condition in CATEGORIES:
        try:
            found = scrape_category(url, platform, condition)
            all_products.extend(found)
            print(f"[{SOURCE}] {platform or 'used category'}: {len(found)} games scanned.")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if not all_products:
        raise SystemExit("[rosegamers_nintendo] No games collected.\n" + "\n".join(errors))

    added = updated = 0
    for item in all_products:
        _, is_new, changed = db.upsert_item({"source": SOURCE, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    available = sum(item["in_stock"] for item in all_products)
    print(f"[{SOURCE}] {len(all_products)} games scanned; {available} available, {added} new, {updated} updated.")
    for error in errors:
        print(f"[{SOURCE}] {error}")


if __name__ == "__main__":
    main()
