"""Minimal, tolerant HTML tree for parsers of public web pages (stdlib only, no network).

:func:`parse_html` builds a tree of :class:`Node` from :mod:`html.parser` events: void
elements never take children, an end tag closes the nearest open element of that name (stray
end tags are ignored), ``<script>`` / ``<style>`` bodies are dropped. Enough structure for
pages whose layout is stable (class names, table rows), without a third-party dependency.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

__all__ = ["Node", "parse_html"]

_VOID: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)  # fmt: skip
_SKIP: Final = frozenset({"script", "style", "noscript", "template"})


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def iter(self) -> Iterator[Node]:
        """This node and all element descendants, document order."""
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        return [
            n
            for n in self.iter()
            if n is not self and (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
        ]

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        found = self.find_all(tag, cls)
        return found[0] if found else None

    def text(self) -> str:
        """Concatenated text, whitespace collapsed."""
        parts: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                else:
                    walk(c)

        walk(self)
        return " ".join(" ".join(parts).replace("\xa0", " ").split())

    def links(self) -> list[str]:
        return [n.attrs["href"] for n in self.iter() if n.tag == "a" and "href" in n.attrs]


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack: list[Node] = [self.root]
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP or self.skip:
            return
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    """Tree of ``text`` (root tag ``#root``)."""
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root
