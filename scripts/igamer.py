#!/usr/bin/env python3
"""Nintendo Switch 1/2 game listings from iGamer."""
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
import db  # noqa: E402
from _scraper_utils import Node, TreeParser, clean, game_title, get_image_url  # noqa: E402

BASE = "https://igamer.ir"
SOURCE = "igamer_nintendo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CATEGORIES = (
    ("https://igamer.ir/buy/بازی-nintendo-switch/", "Switch1"),
    ("https://igamer.ir/buy/بازی-nintendo-switch-2/", "Switch2"),
)


def fetch(url):
    parts = urlsplit(url)
    safe_url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"), parts.query, parts.fragment))
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


def normalize_digits(value):
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def parse_page(html, category_url, platform):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for card in parser.root.find("product-grid-item"):
        links = descendants(card, "a", "product-image-link")
        href = links[0].attrs.get("href", "") if links else ""
        headings = descendants(card, "h3", "wd-entities-title")
        raw_title = clean(headings[0].text()) if headings else clean(links[0].text()) if links else ""
        if not href or not raw_title:
            continue
        title = re.sub(r"^(?:خرید\s+)?بازی\s+", "", raw_title, flags=re.I)
        title = game_title(title)
        title = re.sub(r"\s+(?:برای\s+)?(?:نینتندو\s*)?(?:Nintendo\s*)?(?:Switch|سوییچ|سویچ)\s*[۱۲12]?\s*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s+برای\s*$", "", title, flags=re.I).strip()
        if not title:
            continue
        price_nodes = descendants(card, "span", "woocommerce-Price-amount")
        raw_price = normalize_digits(clean(price_nodes[0].text())) if price_nodes else ""
        amount_match = re.search(r"\d[\d٬,،.\s]*", raw_price)
        digits = re.sub(r"\D", "", amount_match.group(0)) if amount_match else ""
        amount = int(digits) if digits else 0
        in_stock = amount > 0 and "instock" in card.attrs.get("class", "").split()
        products.append({
            "title": title,
            "text": f"قیمت: {amount:,} تومان" if amount else "قیمت: 0 تومان",
            "link": urljoin(BASE, href),
            "condition": "new",
            "in_stock": in_stock,
            "platform": platform,
            "image_url": get_image_url(card, BASE),
        })

    for anchor in descendants(parser.root, "a"):
        href = anchor.attrs.get("href", "")
        absolute = urljoin(BASE, href)
        parsed = urlparse(absolute)
        if parsed.path.rstrip("/") == urlparse(category_url).path.rstrip("/"):
            query = parse_qs(parsed.query)
            # Woodmart can provide numbered pagination or a larger page-size option.
            if query.get("product-page") or query.get("per_page", [""])[0].strip() in {"50", "75", "100"}:
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
