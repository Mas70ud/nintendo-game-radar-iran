#!/usr/bin/env python3
"""Nintendo Switch 1/2 new and second-hand games from TechSiro."""
import re
import sys
import unicodedata
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

BASE = "https://techsiro.com"
NEW_CATEGORIES = (
    ("https://techsiro.com/category/49/games-nintendo-switch", "Switch1"),
    ("https://techsiro.com/category/6/games-nintendo-switch-2", "Switch2"),
)
SECONDHAND_CATEGORY = "https://techsiro.com/category/10/second-hand-game"
NEW_SOURCE = "techsiro_nintendo_new"
USED_SOURCE = "techsiro_nintendo_secondhand"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
TIMEOUT = 15


def fetch(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def walk_nodes(node):
    if isinstance(node, Node):
        yield node
        for child in node.children:
            yield from walk_nodes(child)


def get_attr(node, tag, attr):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag and child.attrs.get(attr):
                return child.attrs[attr]
            value = get_attr(child, tag, attr)
            if value:
                return value
    return ""


def persian_number(value):
    return "".join(str(unicodedata.digit(char)) if char.isdigit() else char for char in value)


def extract_title(card, platform):
    raw = clean(get_attr(card, "img", "alt"))
    if not raw:
        for node in walk_nodes(card):
            if node.tag == "p":
                text = clean(node.text())
                if re.search(r"نینتندو|Nintendo|Switch|سوییچ", text, re.I):
                    raw = text
                    break
    raw = re.sub(r"^بازی\s+(?:کارکرده\s+)?", "", raw, flags=re.I)
    raw = re.sub(r"\s*(?:[-–—]\s*)?(?:کارکرده|استوک|second[ -]?hand)\s*$", "", raw, flags=re.I)
    raw = re.sub(r"\s+برای\s*$", "", raw)
    title = game_title(raw)
    title = re.sub(r"\s+برای\s*$", "", title).strip()
    return title


def parse_page(html, platform=None, condition="new"):
    parser = TreeParser()
    parser.feed(html)
    products = []
    max_page = 1

    for card in walk_nodes(parser.root):
        if card.tag != "a":
            continue
        href = card.attrs.get("href", "")
        if not re.match(r"^/products/\d+(/|$)", href):
            continue

        card_text = clean(card.text())
        image_url = get_image_url(card, BASE)
        img_alt = clean(get_attr(card, "img", "alt"))
        searchable = f"{href} {img_alt} {card_text}".casefold()
        if condition == "used" and not re.search(r"nintendo|switch|نینتندو|سوییچ", searchable):
            # The used-games category contains PlayStation and Xbox products too.
            continue

        if platform is None:
            platform_value = "Switch2" if re.search(r"switch\s*2|switch-2|ns2|سوییچ\s*2|سوییچ\s*۲", searchable) else "Switch1"
        else:
            platform_value = platform

        title = extract_title(card, platform_value)
        if not title:
            continue
        price_match = re.search(r"([\d۰-۹٠-٩][\d۰-۹٠-٩٬,،.\s]*)\s*تومان", card_text, re.I)
        price_text = ""
        amount = 0
        if price_match:
            digits = "".join(str(unicodedata.digit(char)) for char in price_match.group(1) if char.isdigit())
            if digits:
                amount = int(digits)
                price_text = f"{amount:,} تومان"

        unavailable = bool(re.search(r"ناموجود|موجود\s+نیست|اتمام\s+موجودی", card_text))
        in_stock = amount > 0 and not unavailable
        products.append({
            "title": title,
            "text": f"قیمت: {price_text or '0 تومان'}",
            "link": urljoin(BASE, href),
            "condition": condition,
            "in_stock": in_stock,
            "platform": platform_value,
            "image_url": image_url,
        })

    for node in walk_nodes(parser.root):
        if node.tag == "button":
            label = clean(node.text())
            if label.isdigit() or re.fullmatch(r"[۰-۹٠-٩]+", label):
                max_page = max(max_page, int(persian_number(label)))
    return products, max_page


def scrape_category(url, platform=None, condition="new"):
    first, max_page = parse_page(fetch(url), platform, condition)
    pages = [f"{url}?page={number}" for number in range(2, max_page + 1)]
    products = {item["link"]: item for item in first}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for page_products, _ in pool.map(lambda page: parse_page(fetch(page), platform, condition), pages):
            for item in page_products:
                products[item["link"]] = item
    return list(products.values())


def save_products(source, products):
    added = updated = 0
    for item in products:
        _, is_new, changed = db.upsert_item({"source": source, **item})
        added += bool(is_new)
        updated += bool(changed and not is_new)
    available = sum(item["in_stock"] for item in products)
    print(f"[{source}] {len(products)} games scanned; {available} available, {added} new, {updated} updated.")


def main():
    db.init_db()
    errors = []
    new_products = []
    used_products = []
    categories = [(url, platform, "new") for url, platform in NEW_CATEGORIES]
    categories.append((SECONDHAND_CATEGORY, None, "used"))
    # Fetch the three independent catalogs concurrently. Each catalog keeps a
    # smaller page pool to avoid opening too many connections to TechSiro.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [(url, platform, condition,
                    pool.submit(scrape_category, url, platform, condition))
                   for url, platform, condition in categories]
        for url, platform, condition, future in futures:
            try:
                page_products = future.result()
                if condition == "used":
                    used_products = page_products
                else:
                    new_products.extend(page_products)
                print(f"[techsiro_nintendo] {platform or 'Second-hand'} {condition}: {len(page_products)} games scanned.")
            except Exception as exc:
                errors.append(f"{platform or 'Second-hand'}: {exc}")

    if not new_products and not used_products:
        raise SystemExit("[techsiro] No Nintendo games collected.\n" + "\n".join(errors))
    save_products(NEW_SOURCE, new_products)
    save_products(USED_SOURCE, used_products)
    for error in errors:
        print(f"[techsiro] {error}")


if __name__ == "__main__":
    main()
