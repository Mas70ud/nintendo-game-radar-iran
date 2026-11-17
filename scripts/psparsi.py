#!/usr/bin/env python3
"""Nintendo Switch listings from PSParsi."""
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

SOURCES = (
    ("psparsi_nintendo", "new"),
    ("psparsi_secondhand", "used"),
)
BASE = "https://psparsi.ir"
CATEGORY = (
    "https://psparsi.ir/product-category/games/"
    "%D8%A8%D8%A7%D8%B2%DB%8C-%D9%87%D8%A7%DB%8C-nintendo-switch/"
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
TIMEOUT = 15
CATEGORY_SECONDHAND = (
    "https://psparsi.ir/product-category/games/"
    "%d8%a8%d8%a7%d8%b2%db%8c-%d9%87%d8%a7%db%8c-%da%a9%d8%a7%d8%b1%da%a9%d8%b1%d8%af%d9%87/"
    "%d8%a8%d8%a7%d8%b2%db%8c-%d9%87%d8%a7%db%8c-%da%a9%d8%a7%d8%b1%da%a9%d8%b1%d8%af%d9%87-%d9%86%db%8c%d9%86%d8%aa%d9%86%d8%af%d9%88-%d8%b3%d9%88%d8%A6%DB%8C%da%86/"
)


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


def parse_page(html, condition="new"):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for card in parser.root.find("prob-item"):
        title_nodes = card.find("woocommerce-loop-product_title")
        source_title = clean(title_nodes[0].text()) if title_nodes else clean(get_attr(card, "img", "alt"))
        source_title = re.sub(r"\s*[–—-]\s*(?:کارکرده|استوک|second[ -]?hand)\s*$", "", source_title, flags=re.I)
        title = game_title(source_title)
        link = get_attr(card, "a", "href")
        if not title or not link:
            continue

        price_nodes = card.find("price_onsale_ar") or card.find("price")
        price = clean(price_nodes[0].text()) if price_nodes else ""
        # PSParsi places the currency before the number ("تومان 9,500,000").
        amount = re.search(r"[\d۰-۹][\d۰-۹٬,\.\s]*", price)
        digits = re.sub(r"\D", "", amount.group(0)) if amount else ""
        if digits:
            price_text = f"{int(digits):,} تومان"
        else:
            price_text = ""
        classes = card.attrs.get("class", "").split()
        stock_words = " ".join(clean(n.text()) for n in card.find("stock"))
        in_stock = "instock" in classes or "موجود" in stock_words
        if "outofstock" in classes or "ناموجود" in stock_words or not digits or int(digits) <= 0:
            in_stock = False

        platform = "Switch2" if re.search(r"switch\s*2|سوییچ\s*2|سوییچ\s*۲", source_title, re.I) else "Switch1"
        products.append({
            "title": title, "text": f"قیمت: {price_text}", "link": urljoin(BASE, link),
            "condition": condition, "in_stock": in_stock, "platform": platform,
            "image_url": get_image_url(card, BASE),
        })

    def walk(node):
        if node.tag == "a":
            href = node.attrs.get("href", "")
            if "/page/" in href and re.search(r"/page/\d+/?(?:\?|$)", href):
                pages.add(urljoin(BASE, href))
        for child in node.children:
            if isinstance(child, Node):
                walk(child)
    walk(parser.root)
    return products, pages


def scrape(category=CATEGORY, condition="new"):
    first, next_pages = parse_page(fetch(category), condition)
    # The first page exposes the full pagination; fetch the remaining pages in
    # a small pool so scheduled refreshes stay within the collector time limit.
    max_page = max((int(re.search(r"/page/(\d+)", page).group(1)) for page in next_pages), default=1)
    # Preserve the site's percent-encoded Persian category while inserting the
    # standard WooCommerce page segment.
    base_path = category.rstrip("/")
    pages = [f"{base_path}/page/{number}/" for number in range(2, max_page + 1)]
    found = {item["link"]: item for item in first}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for products, _ in pool.map(lambda url: parse_page(fetch(url), condition), pages):
            for item in products:
                found[item["link"]] = item
    return list(found.values())


def run_source(source, category, condition):
    db.init_db()
    products = scrape(category, condition)
    if not products:
        raise SystemExit(f"[{source}] هیچ محصولی از صفحهٔ PSParsi دریافت نشد.")
    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": source, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    available = sum(item["in_stock"] for item in products)
    print(f"[{source}] {len(products)} بازی بررسی شد؛ {available} موجود، {added} جدید، {updated} به‌روزرسانی شد.")


def main():
    # Both categories are independent. Collect them together so a slow page
    # in one category does not make the other wait for a full crawl first.
    def collect(source, condition):
        category = CATEGORY if condition == "new" else CATEGORY_SECONDHAND
        return source, condition, scrape(category, condition)

    db.init_db()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [(source, pool.submit(collect, source, condition))
                   for source, condition in SOURCES]
        for source, future in futures:
            try:
                source, condition, products = future.result()
                if not products:
                    print(f"[{source}] هیچ محصولی از صفحهٔ PSParsi دریافت نشد.")
                    continue
                added = updated = 0
                for item in products:
                    _, is_new, changed = db.upsert_item({"source": source, **item})
                    added += bool(is_new)
                    updated += bool(changed and not is_new)
                available = sum(item["in_stock"] for item in products)
                print(f"[{source}] {len(products)} بازی بررسی شد؛ {available} موجود، {added} جدید، {updated} به‌روزرسانی شد.")
            except Exception as exc:
                print(f"[{source}] Error: {exc}")


if __name__ == "__main__":
    main()
