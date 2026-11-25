#!/usr/bin/env python3
"""Shared HTML and product helpers used by the independent store collectors."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

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

