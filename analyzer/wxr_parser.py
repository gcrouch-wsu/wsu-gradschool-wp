from __future__ import annotations

from collections import defaultdict
import mimetypes
from typing import BinaryIO

from defusedxml import ElementTree as ET

from .ids import wordpress_id
from .models import Author, ContentItem, Term, WXRExport


MAX_EXPORT_ITEMS = 40_000


CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
EXCERPT_NAMESPACE = "http://wordpress.org/export/1.2/excerpt/"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element, local_name: str, default: str = "") -> str:
    for child in element:
        if _local_name(child.tag) == local_name:
            return child.text or default
    return default


def _namespaced_text(element, namespace: str, local_name: str) -> str:
    child = element.find(f"{{{namespace}}}{local_name}")
    return child.text if child is not None and child.text else ""


def _find_channel(root):
    if _local_name(root.tag) == "channel":
        return root
    for child in root:
        if _local_name(child.tag) == "channel":
            return child
    return None


def parse_wxr(file_stream: BinaryIO) -> WXRExport:
    """Parse a WordPress WXR export into normalized in-memory records."""
    tree = ET.parse(file_stream)
    root = tree.getroot()
    channel = _find_channel(root)
    if channel is None:
        raise ValueError("This does not appear to be a WordPress export: <channel> is missing.")

    authors: dict[str, Author] = {}
    for element in channel:
        if _local_name(element.tag) != "author":
            continue
        login = _text(element, "author_login")
        if not login:
            continue
        authors[login] = Author(
            login=login,
            display_name=_text(element, "author_display_name"),
            email=_text(element, "author_email"),
            first_name=_text(element, "author_first_name"),
            last_name=_text(element, "author_last_name"),
        )

    site_title = _text(channel, "title")
    site_url = _text(channel, "site_url") or _text(channel, "link")
    home_url = _text(channel, "base_site_url") or _text(channel, "link")
    description = _text(channel, "description")

    items: list[ContentItem] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for element in channel:
        if _local_name(element.tag) != "item":
            continue

        post_id = wordpress_id(_text(element, "post_id"))
        post_type = _text(element, "post_type")
        if not post_id or not post_type:
            warnings.append("Skipped an export item with no canonical WordPress ID or post type.")
            continue
        if post_id in seen_ids:
            warnings.append(f"Duplicate WordPress item ID {post_id} was found; URL resolution may be ambiguous.")
        seen_ids.add(post_id)
        if len(items) >= MAX_EXPORT_ITEMS:
            raise ValueError(
                f"This export has more than {MAX_EXPORT_ITEMS:,} records and exceeds the analyzer bound."
            )

        author_login = _text(element, "creator")
        author = authors.get(author_login, Author(login=author_login))
        author_name = author.display_name or " ".join(
            part for part in (author.first_name, author.last_name) if part
        ) or author_login
        attachment_url = _text(element, "attachment_url")
        mime_type = _text(element, "post_mime_type")
        if post_type == "attachment" and not mime_type and attachment_url:
            mime_type = mimetypes.guess_type(attachment_url)[0] or ""

        meta: dict[str, list[str]] = defaultdict(list)
        terms: list[Term] = []
        for child in element:
            child_name = _local_name(child.tag)
            if child_name == "postmeta":
                key = _text(child, "meta_key")
                if key:
                    meta[key].append(_text(child, "meta_value"))
            elif child_name == "category":
                taxonomy = child.attrib.get("domain", "category")
                slug = child.attrib.get("nicename", "")
                terms.append(Term(taxonomy=taxonomy, slug=slug, name=child.text or slug))

        items.append(
            ContentItem(
                id=post_id,
                post_type=post_type,
                status=_text(element, "status"),
                title=_text(element, "title") or "Untitled",
                slug=_text(element, "post_name"),
                url=_text(element, "link"),
                author_login=author_login,
                author_name=author_name,
                author_email=author.email,
                created=_text(element, "post_date"),
                created_gmt=_text(element, "post_date_gmt"),
                modified=_text(element, "post_modified"),
                modified_gmt=_text(element, "post_modified_gmt"),
                parent_id=_text(element, "post_parent") or "0",
                mime_type=mime_type,
                attachment_url=attachment_url,
                excerpt=_namespaced_text(element, EXCERPT_NAMESPACE, "encoded"),
                content=_namespaced_text(element, CONTENT_NAMESPACE, "encoded"),
                terms=terms,
                meta=dict(meta),
            )
        )

    return WXRExport(
        site_title=site_title,
        site_url=site_url,
        home_url=home_url,
        description=description,
        authors=authors,
        items=items,
        warnings=warnings,
    )
