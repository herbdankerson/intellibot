"""Utilities for fetching and normalising web content to Markdown."""

from __future__ import annotations

import hashlib
import re
from typing import List, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Comment
from slugify import slugify

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class WebScrapeError(RuntimeError):
    """Raised when a scrape attempt fails."""


def _normalise_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url.lstrip('/')}"
    return url


def fetch_html(url: str) -> Tuple[str, str]:
    """Return the HTML payload and final URL after redirects."""

    normalised = _normalise_url(url)
    response = requests.get(
        normalised,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.text, response.url


def slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    path = parsed.path.strip("/")
    base = host
    if path:
        base = f"{base}-{path.replace('/', '-')}"
    slug = slugify(base)
    if parsed.query:
        query_hash = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{query_hash}" if slug else query_hash
    return slug or "web-source"


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    if soup.body is None:
        body = soup.new_tag("body")
        body.append(soup)
        soup.body = body
    removable = [
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "header",
        "nav",
        "footer",
        "form",
        "aside",
    ]
    for element in soup.body.find_all(removable):
        element.decompose()
    for comment in soup.body.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    return soup


_CONTENT_RE = re.compile(r"(content|main|primary|article|body|statute|highlight)", re.I)


def _find_content_root(soup: BeautifulSoup):
    body = soup.body or soup
    for selector in ("main", "article"):
        node = body.find(selector)
        if node:
            return node
    node = body.find(attrs={"role": "main"})
    if node:
        return node
    for attr in ("id", "class"):
        node = body.find(attrs={attr: _CONTENT_RE})
        if node:
            return node
    return body


def _render_statute_sections(root: BeautifulSoup) -> List[str]:
    sections = root.select("div.Section")
    if not sections:
        return []

    lines: List[str] = []
    for section in sections:
        number_node = section.select_one("span.SectionNumber")
        catchline_node = section.select_one("span.CatchlineText")
        header_parts = []
        if number_node and number_node.get_text(strip=True):
            header_parts.append(number_node.get_text(strip=True))
        if catchline_node and catchline_node.get_text(strip=True):
            header_parts.append(catchline_node.get_text(strip=True))
        if header_parts:
            lines.append("## " + " ".join(header_parts))

        body = section.select_one("span.SectionBody")
        if body:
            intro_parts: List[str] = []
            for child in body.children:
                if getattr(child, "get", None) is None:
                    text = str(child).strip()
                    if text:
                        intro_parts.append(text)
                    continue
                classes = child.get("class", [])
                if isinstance(classes, list) and any(
                    "Subsection" in c or c == "Subsection" for c in classes
                ):
                    break
                if child.name == "span":
                    text = child.get_text(" ", strip=True)
                    if text:
                        intro_parts.append(text)
            if intro_parts:
                lines.append("\n".join(intro_parts))

            for subsection in body.select("div.Subsection"):
                number = subsection.select_one("span.Number")
                texts = [
                    node.get_text(" ", strip=True)
                    for node in subsection.select("span.Text")
                    if node.get_text(" ", strip=True)
                ]
                if not texts:
                    continue
                prefix = number.get_text(" ", strip=True) if number else ""
                content = " ".join(texts)
                lines.append(f"{prefix} {content}".strip())

        history = section.select_one("div.History")
        if history:
            history_text = history.get_text(" ", strip=True)
            if history_text:
                lines.append(history_text)

    return lines


def html_to_markdown(html: str) -> Tuple[str, str]:
    """Convert raw HTML into a title + Markdown string."""

    soup = _clean_soup(html)
    title_text = ""
    if soup.title and soup.title.string:
        title_text = soup.title.string.strip()

    block_tags = {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "li",
        "blockquote",
        "pre",
    }
    lines: List[str] = []
    if title_text:
        lines.append(f"# {title_text}")

    root = _find_content_root(soup)
    statute_lines = _render_statute_sections(root)
    if statute_lines:
        lines.extend(statute_lines)
    else:
        for element in root.find_all(block_tags.union({"div"})):
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            if element.name.startswith("h"):
                level = min(int(element.name[1]), 6)
                lines.append("#" * level + f" {text}")
            elif element.name == "li":
                lines.append(f"- {text}")
            elif element.name == "blockquote":
                lines.append(f"> {text}")
            elif element.name == "pre":
                lines.append("```\n" + text + "\n```")
            else:
                lines.append(text)

    markdown = "\n\n".join(lines)
    return title_text, markdown


def scrape_markdown(url: str) -> Tuple[str, str, str]:
    """Fetch a URL and return (final_url, title, markdown)."""

    html, final_url = fetch_html(url)
    title, markdown = html_to_markdown(html)
    return final_url, title, markdown
