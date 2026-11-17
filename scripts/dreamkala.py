#!/usr/bin/env python3
"""Nintendo Switch 1 and Switch 2 listings from DreamKala."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(WEBAPP_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
import db  # noqa: E402
from _scraper_utils import Node, TreeParser, clean, game_title, get_image_url  # noqa: E402

SOURCE = "dreamkala_nintendo"
BASE = "https://dreamkala.com/"
CATEGORIES = (
    ("https://dreamkala.com/NintendoSwitch-Games", "Switch1"),
    ("https://dreamkala.com/Nintendo-Switch2-Games", "Switch2"),
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
TIMEOUT = 40


def fetch(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def get_attr(node, tag, attr):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag and child.attrs.get(attr):
                return child.attrs[attr]
            value = get_attr(child, tag, attr)
            if value:
                return value
    return ""


def parse_page(html, category_url, platform):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    category_path = category_url.rstrip("/")

    for card in parser.root.find("product-layout"):
        link = get_attr(card, "a", "href")
        # The page contains unrelated recommendation cards; accept only cards
        # whose product URLs belong to the requested console category.
        if not link or not link.startswith(category_path + "/"):
            continue
        title_nodes = card.find("name")
        raw_title = clean(title_nodes[0].text()) if title_nodes else clean(get_attr(card, "img", "alt"))
        title = game_title(raw_title)
        if not title:
            continue

        price_nodes = card.find("price-normal")
        price = clean(price_nodes[0].text()) if price_nodes else ""
        amount = re.search(r"[\d۰-۹][\d۰-۹٬,\.\s]*", price)
        digits = re.sub(r"\D", "", amount.group(0)) if amount else ""
        has_price = bool(digits and int(digits) > 0)
        classes = card.attrs.get("class", "").split()
        in_stock = has_price and "out-of-stock" not in classes
        if "in-stock" in classes:
            in_stock = has_price

        products.append({
            "title": title,
            "text": f"قیمت: {int(digits):,} تومان" if has_price else "قیمت: 0 تومان",
            "link": urljoin(BASE, link),
            "condition": "new",
            "in_stock": in_stock,
            "platform": platform,
            "image_url": get_image_url(card, BASE),
        })

    def walk(node):
        if node.tag == "a":
            href = node.attrs.get("href", "")
            absolute = urljoin(BASE, href)
            if absolute.startswith(category_path + "?"):
                query_page = re.search(r"[?&]page=(\d+)", absolute)
                if query_page:
                    pages.add(int(query_page.group(1)))
        for child in node.children:
            if isinstance(child, Node):
                walk(child)

    walk(parser.root)
    return products, pages


def scrape_category(category_url, platform):
    first, page_numbers = parse_page(fetch(category_url), category_url, platform)
    urls = [f"{category_url}?page={number}" for number in sorted(page_numbers) if number > 1]
    found = {item["link"]: item for item in first}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for products, _ in pool.map(lambda url: parse_page(fetch(url), category_url, platform), urls):
            for item in products:
                found[item["link"]] = item
    return list(found.values())


def main():
    db.init_db()
    products = []
    errors = []
    for category_url, platform in CATEGORIES:
        try:
            products.extend(scrape_category(category_url, platform))
        except Exception as exc:  # Keep one category failure from blocking the other.
            errors.append(f"{platform}: {exc}")

    if not products:
        details = "\n".join(errors) if errors else "No products found or page structure changed."
        raise SystemExit(f"[{SOURCE}] No games collected.\n{details}")

    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": SOURCE, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    available = sum(item["in_stock"] for item in products)
    print(f"[{SOURCE}] {len(products)} games scanned; {available} available, {added} new, {updated} updated.")
    for error in errors:
        print(f"[{SOURCE}] {error}")


if __name__ == "__main__":
    main()
