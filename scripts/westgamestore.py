#!/usr/bin/env python3
"""Nintendo Switch 1/2 game listings from WestGameStore."""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPTS_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(WEBAPP_DIR))
import db  # noqa: E402

BASE = "https://westgamestore.ir"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
TIMEOUT = 30


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return " ".join((c if isinstance(c, str) else c.text()) for c in self.children)

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def find(self, class_name):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if child.has_class(class_name):
                    found.append(child)
                found.extend(child.find(class_name))
        return found


class TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].children.append(data)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def game_title(value):
    """Remove a trailing console label while preserving names such as 1-2 Switch."""
    value = clean(value)
    value = re.sub(
        r"\s*(?:[-–—]\s*(?:nintendo\s*)?switch(?:\s*[12])?(?:\s+(?:edition|game|games|exclusive))?"
        r"|\s+nintendo\s+switch(?:\s*[12])?(?:\s+(?:edition|game|games|exclusive))?)\s*$",
        "", value, flags=re.IGNORECASE,
    )
    return value.rstrip(" -–—:").strip()


def get_image_url(node, base_url):
    """Return the highest quality product thumbnail, including lazy-loaded images."""
    images = []

    def walk(current):
        if isinstance(current, Node):
            if current.tag == "img":
                attrs = current.attrs
                candidates = [attrs.get(key, "") for key in
                              ("data-src", "data-lazy-src", "data-original", "data-srcset", "srcset", "src")]
                for candidate in candidates:
                    if not candidate:
                        continue
                    # Pick the largest entry from srcset when only a srcset is available.
                    if " " in candidate and ("," in candidate or re.search(r"\s+\d+(?:\.\d+)?w", candidate)):
                        candidate = candidate.split(",")[-1].strip().split()[0]
                    candidate = candidate.strip()
                    if candidate and not candidate.startswith("data:"):
                        images.append(candidate)
                        break
            for child in current.children:
                walk(child)

    walk(node)
    return urljoin(base_url, images[0]) if images else ""


def fetch(url):
    parts = urlsplit(url)
    url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"), parts.query, parts.fragment))
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


def parse_page(html, condition):
    parser = TreeParser()
    parser.feed(html)
    products, pages = [], set()
    for layout in parser.root.find("product-layout"):
        title_nodes = layout.find("name")
        source_title = clean(title_nodes[0].text()) if title_nodes else clean(get_attr(layout, "img", "alt"))
        title = game_title(source_title)
        if not title:
            continue
        model = clean(" ".join(n.text() for n in layout.find("stat-2")))
        link = get_attr(layout, "a", "href")
        searchable = f"{source_title} {model} {link}".casefold()
        # The used-games listing also contains PS/Xbox items; keep Switch games only.
        if not re.search(r"nintendo|switch|نینتندو|سوییچ", searchable):
            continue
        price_nodes = layout.find("price-normal")
        price = clean(price_nodes[0].text()) if price_nodes else "قیمت درج نشده"
        classes = layout.attrs.get("class", "").split()
        digits = re.sub(r"\D", "", price)
        has_price = bool(digits and int(digits) > 0)
        if "in-stock" in classes:
            in_stock = has_price
        elif "out-of-stock" in classes or "has-zero-price" in classes:
            in_stock = False
        else:
            # Category pages omit stock classes; a positive price is the
            # store's available listing signal. Zero/blank prices are hidden.
            in_stock = has_price
        platform = "Switch2" if re.search(r"switch\s*2|سوییچ\s*2|سوییچ\s*۲|نینتندو\s*2", searchable) else "Switch1"
        card_text = f"قیمت: {price}"
        products.append({"title": title, "text": card_text, "link": urljoin(BASE, link),
                         "condition": condition,
                         "in_stock": in_stock, "platform": platform,
                         "image_url": get_image_url(layout, BASE)})

    # Follow listing pagination links so the full catalog is scanned.
    def walk(node):
        if node.tag == "a":
            href = node.attrs.get("href", "")
            query = parse_qs(urlparse(href).query)
            if "page" in query and href:
                pages.add(urljoin(BASE, href))
        for child in node.children:
            if isinstance(child, Node):
                walk(child)
    walk(parser.root)
    return products, pages


def scrape_listing(url, condition):
    queue, visited, found = [url], set(), {}
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        products, pages = parse_page(fetch(current), condition)
        for product in products:
            # The new/used page can overlap; keep the used label when an item appears there.
            found[product["link"] or product["title"]] = product
        queue.extend(page for page in pages if page not in visited)
    return list(found.values()), len(visited)


SOURCES = (
    ("westgamestore_new", "https://westgamestore.ir/%D8%A8%D8%A7%D8%B2%DB%8C-ps4-xbox/%D8%A8%D8%A7%D8%B2%DB%8C-nintendo-switch", "new"),
    ("westgamestore_secondhand", "https://westgamestore.ir/%D8%A8%D8%A7%D8%B2%DB%8C-ps4-xbox/%D8%A8%D8%A7%D8%B2%DB%8C-%DA%AF%DB%8C%D9%85-%D8%AF%D8%B3%D8%AA%D9%87-%D8%AF%D9%88%D9%85-%DA%A9%D8%A7%D8%B1%DA%A9%D8%B1%D8%AF%D9%87-ps4-xbox-one", "used"),
)


def main():
    db.init_db()
    for source, url, condition in SOURCES:
        try:
            listings, page_count = scrape_listing(url, condition)
            if not listings:
                # An empty Nintendo filter is a valid completed scan for this
                # mixed PS/Xbox category. Report it clearly without marking a
                # successful HTTP fetch as a site execution failure.
                print(f"[{source}] Scan completed: {page_count} page(s), 0 Nintendo Switch games listed.")
                continue
            added = updated = 0
            for item in listings:
                _, is_new, changed = db.upsert_item({"source": source, **item})
                added += bool(is_new)
                updated += bool(changed and not is_new)
            print(f"[{source}] {len(listings)} games scanned across {page_count} page(s); {added} new, {updated} updated.")
        except Exception as exc:
            print(f"[{source}] Error: {exc}")


if __name__ == "__main__":
    main()
