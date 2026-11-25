#!/usr/bin/env python3
"""Nintendo Switch 1 and Switch 2 games from NakhlMarket."""
import re
import sys
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
import db  # noqa: E402
from _scraper_utils import Node, TreeParser, clean, game_title, get_image_url  # noqa: E402

BASE = "https://nakhlmarket.com"
SOURCE = "nakhlmarket_nintendo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CATEGORIES = (
    (f"{BASE}/product-category/game/nintendo_games/nintendo-cartridges/", "Switch1"),
    (f"{BASE}/product-category/game/nintendo_games/nintendo-switch2-cartridges/", "Switch2"),
)


def fetch(url):
    parts = urlsplit(url)
    safe_url = urlunsplit((parts.scheme, parts.netloc,
                           quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                           parts.query, parts.fragment))
    request = Request(safe_url, headers={"User-Agent": USER_AGENT,
                                         "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def descendants(node, tag=None, class_name=None):
    result = []
    for child in node.children:
        if isinstance(child, Node):
            if (tag is None or child.tag == tag) and (class_name is None or child.has_class(class_name)):
                result.append(child)
            result.extend(descendants(child, tag, class_name))
    return result


def parse_digits(value):
    value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    match = re.search(r"\d[\d٬,،.\s]*", value)
    return re.sub(r"\D", "", match.group(0)) if match else ""


def parse_page(html, category_url, platform):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for card in parser.root.find("nm-product-box"):
        links = descendants(card, "a")
        product_link = next((a.attrs.get("href", "") for a in links
                             if "/product/" in a.attrs.get("href", "")), "")
        if not product_link:
            continue
        title_nodes = card.find("nm-product-box-title")
        raw_title = clean(title_nodes[0].text()) if title_nodes else clean(descendants(card, "img")[0].attrs.get("alt", "")) if descendants(card, "img") else ""
        if not raw_title:
            continue
        title = re.sub(r"^بازی\s+", "", raw_title, flags=re.I)
        title = game_title(title)
        title = re.sub(r"\s+(?:برای\s+)?(?:Nintendo\s*)?Switch\s*[۱۲12]?\s*$", "", title, flags=re.I).strip()
        if not title:
            continue
        price_nodes = card.find("woocommerce-Price-amount")
        # Sale products render regular price first and the payable sale price second.
        price_text = clean(price_nodes[-1].text()) if price_nodes else ""
        digits = parse_digits(price_text)
        amount = int(digits) if digits else 0
        card_classes = card.attrs.get("class", "").casefold()
        availability_text = card.text().casefold()
        unavailable = "outofstock" in card_classes or bool(re.search(r"ناموجود|اتمام موجودی|موجود نیست", availability_text))
        products.append({
            "title": title,
            "text": f"قیمت: {amount:,} تومان" if amount else "قیمت: 0 تومان",
            "link": urljoin(BASE, product_link),
            "condition": "new",
            "in_stock": amount > 0 and not unavailable,
            "platform": platform,
            "image_url": get_image_url(card, BASE),
        })

    base_path = urlparse(category_url).path.rstrip("/")
    for anchor in descendants(parser.root, "a"):
        href = anchor.attrs.get("href", "")
        absolute = urljoin(BASE, href)
        path = urlparse(absolute).path.rstrip("/")
        if path.startswith(base_path + "/page/"):
            pages.add(absolute)
    return products, pages


def scrape_category(url, platform):
    queue, visited, found = [url], set(), {}
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        products, pages = parse_page(fetch(current), url, platform)
        for item in products:
            found[item["link"]] = item
        queue.extend(page for page in pages if page not in visited)
    return list(found.values())


def main():
    db.init_db()
    products, errors = [], []
    for url, platform in CATEGORIES:
        try:
            found = scrape_category(url, platform)
            products.extend(found)
            print(f"[{SOURCE}] {platform}: {len(found)} games scanned.")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if not products:
        raise SystemExit(f"[{SOURCE}] No games collected.\n" + "\n".join(errors))
    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": SOURCE, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    print(f"[{SOURCE}] {len(products)} games scanned; {sum(item['in_stock'] for item in products)} available, {added} new, {updated} updated.")
    for error in errors:
        print(f"[{SOURCE}] {error}")


if __name__ == "__main__":
    main()
