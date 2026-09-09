from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Author:
    login: str
    display_name: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""


@dataclass(frozen=True)
class Term:
    taxonomy: str
    slug: str
    name: str


@dataclass
class ContentItem:
    id: str
    post_type: str
    status: str
    title: str
    slug: str
    url: str
    author_login: str
    author_name: str
    author_email: str
    created: str
    created_gmt: str
    modified: str
    modified_gmt: str
    parent_id: str
    mime_type: str
    attachment_url: str
    excerpt: str
    content: str
    terms: list[Term] = field(default_factory=list)
    meta: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Reference:
    source_id: str
    target_id: str
    kind: str
    field: str
    strength: str
    evidence: str


@dataclass
class WXRExport:
    site_title: str
    site_url: str
    home_url: str
    description: str
    authors: dict[str, Author]
    items: list[ContentItem]
    warnings: list[str] = field(default_factory=list)
