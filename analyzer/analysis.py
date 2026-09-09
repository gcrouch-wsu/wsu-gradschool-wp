from __future__ import annotations

import html
import re
from collections import Counter, defaultdict, deque
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .ids import wordpress_id
from .models import ContentItem, Reference, WXRExport
from .url_normalizer import media_variant_keys, normalize_url, public_href, url_keys
from .wp_rest import wp_admin_edit_url


URL_RE = re.compile(r"(?:https?:)?//[^\s\"'<>\\]+", re.IGNORECASE)
WP_CLASS_ID_RE = re.compile(r"\bwp-(?:image|attachment)-(\d+)\b", re.IGNORECASE)
GALLERY_RE = re.compile(r"\[gallery[^\]]*\bids=[\"']([^\"']+)", re.IGNORECASE)
BLOCK_RE = re.compile(
    r"<!--\s*wp:(?:image|file|audio|video|cover|media-text|gallery)\s+({.*?})\s*/?-->",
    re.IGNORECASE | re.DOTALL,
)
BLOCK_ID_RE = re.compile(r'\"id\"\s*:\s*(\d+)', re.IGNORECASE)
BLOCK_IDS_RE = re.compile(r'\"ids\"\s*:\s*\[([^\]]*)\]', re.IGNORECASE)
REUSABLE_BLOCK_RE = re.compile(
    r"<!--\s*wp:(?:block|core/block)\s+({.*?})\s*/?-->",
    re.IGNORECASE | re.DOTALL,
)
BLOCK_REF_RE = re.compile(r'\"ref\"\s*:\s*(\d+)', re.IGNORECASE)
TABLEPRESS_RE = re.compile(r"\[table(?:press)?[^\]]*\bid\s*=\s*[\"']?(\d+)", re.IGNORECASE)
GRAVITY_FORM_RE = re.compile(r"\[gravityform(?:s)?[^\]]*\bid\s*=\s*[\"']?(\d+)", re.IGNORECASE)
DOCUMENT_SHORTCODE_RE = re.compile(r"\[(?:document|wsu_document)[^\]]*\bid\s*=\s*[\"']?(\d+)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")
MAX_BODY_SCAN = 1_000_000

STRUCTURAL_POST_TYPES = {
    "nav_menu_item",
    "revision",
    "custom_css",
    "customize_changeset",
    "wp_global_styles",
    "wp_template",
    "wp_template_part",
    "wp_navigation",
    "wp_font_family",
    "wp_font_face",
    "oembed_cache",
}

TAXONOMY_LABELS = {
    "category": "Site Categories",
    "post_tag": "University Tags",
    "wsuwp_university_category": "University Categories",
    "wsuwp_university_location": "University Locations",
    "wsuwp_university_org": "University Organizations",
    "tribe_events_cat": "Event Categories",
    "gs-degree-type": "Degree Types",
    "gs-program-name": "Program Names",
    "workflow_state": "Workflow States",
    "nav_menu": "Navigation Menus",
}

GROUP_CONFIG = {
    "posts": {
        "label": "Posts",
        "description": "Editorial news and announcements",
        "order": 10,
    },
    "pages": {
        "label": "Pages",
        "description": "Site pages and landing content",
        "order": 20,
    },
    "events": {
        "label": "Events",
        "description": "The Events Calendar records",
        "order": 30,
    },
    "media": {
        "label": "Media",
        "description": "WordPress Media Library attachments",
        "order": 40,
    },
    "documents": {
        "label": "Managed Documents",
        "description": "WSU document custom-post records",
        "order": 50,
    },
    "forms": {
        "label": "Forms",
        "description": "Form-associated exported post records",
        "order": 60,
    },
    "tablepress": {
        "label": "TablePress",
        "description": "TablePress table records",
        "order": 70,
    },
    "factsheets": {
        "label": "Graduate Factsheets",
        "description": "Graduate program factsheet records",
        "order": 80,
    },
    "other": {
        "label": "Other Content",
        "description": "Plugin and supporting content types",
        "order": 90,
    },
}

# Published records of these types are reachable from their native archives.
ARCHIVE_ENTRY_TYPES = {
    "tribe_events": "Events Calendar archive",
    "gs-factsheet": "Graduate factsheet archive",
}

SERIALIZED_INT_RE = re.compile(r's:(\d+):"(?P<key>[^"]+)";i:(?P<value>\d+);')
SERIALIZED_STRING_RE = re.compile(
    r's:(\d+):"(?P<key>[^"]+)";s:(\d+):"(?P<value>[^"]*)";'
)


class _ReferenceHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.lower(): value for key, value in attrs if value}
        tag = tag.lower()
        if tag in {"a", "area"} and values.get("href"):
            self.urls.append((values["href"], "content-link"))
        if tag in {"img", "audio", "video", "source", "iframe", "embed"} and values.get("src"):
            self.urls.append((values["src"], "content-embed"))
        if tag == "video" and values.get("poster"):
            self.urls.append((values["poster"], "content-embed"))
        if tag == "object" and values.get("data"):
            self.urls.append((values["data"], "content-embed"))
        if values.get("srcset"):
            for candidate in values["srcset"].split(","):
                url = candidate.strip().split(" ", 1)[0]
                if url:
                    self.urls.append((url, "content-embed"))


def _compact_evidence(value: str, limit: int = 180) -> str:
    compact = " ".join(html.unescape(value or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _item_urls(item: ContentItem) -> set[str]:
    return {value for value in (item.url, item.attachment_url) if value}


def _content_class(item: ContentItem) -> str:
    if item.post_type != "attachment":
        return item.post_type
    mime = item.mime_type.lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("application/") or mime.startswith("text/"):
        return "document"
    return "media"


def _content_group(item: ContentItem) -> str:
    if "_gform-form-id" in item.meta:
        return "forms"
    return {
        "post": "posts",
        "page": "pages",
        "tribe_events": "events",
        "attachment": "media",
        "document": "documents",
        "tablepress_table": "tablepress",
        "gs-factsheet": "factsheets",
    }.get(item.post_type, "other")


def _attachment_details(item: ContentItem) -> dict:
    if item.post_type != "attachment":
        return {}

    metadata = (item.meta.get("_wp_attachment_metadata") or [""])[0]
    integers: dict[str, int] = {}
    strings: dict[str, list[str]] = defaultdict(list)
    for match in SERIALIZED_INT_RE.finditer(metadata):
        integers.setdefault(match.group("key"), int(match.group("value")))
    for match in SERIALIZED_STRING_RE.finditer(metadata):
        strings[match.group("key")].append(match.group("value"))

    url_path = urlsplit(item.attachment_url).path
    file_name = PurePosixPath(url_path).name
    derivative_files = sorted(
        {
            value
            for value in strings.get("file", [])
            if value and PurePosixPath(value).name != file_name
        }
    )
    return {
        "file_name": file_name,
        "file_size": integers.get("filesize"),
        "width": integers.get("width"),
        "height": integers.get("height"),
        "stored_path": (strings.get("file") or [""])[0],
        "derivative_files": derivative_files,
        "derivative_count": len(derivative_files),
        "alt_text": (item.meta.get("_wp_attachment_image_alt") or [""])[0],
        "caption": item.excerpt,
        "description": item.content,
    }


def _is_expected_author(item: ContentItem, aliases: set[str]) -> bool:
    values = {item.author_login.casefold().strip(), item.author_name.casefold().strip()}
    return bool(values & aliases)


def analyze_export(
    export: WXRExport,
    expected_author_aliases: set[str] | None = None,
) -> dict:
    """Build a link graph and return an explainable, browser-ready report."""
    expected_aliases = {
        alias.casefold().strip()
        for alias in (expected_author_aliases or {"greg crouch", "gcrouch"})
        if alias.strip()
    }
    base_url = export.site_url or export.home_url
    by_id = {item.id: item for item in export.items}
    reportable = {
        item.id: item
        for item in export.items
        if item.post_type not in STRUCTURAL_POST_TYPES
    }

    url_map: dict[str, set[str]] = defaultdict(set)
    for item in reportable.values():
        for raw_url in _item_urls(item):
            keys = media_variant_keys(raw_url, base_url) if item.post_type == "attachment" else url_keys(raw_url, base_url)
            for key in keys:
                url_map[key].add(item.id)

    references: set[Reference] = set()
    roots: set[str] = set()
    unresolved_internal: set[str] = set()
    site_hosts = {
        urlsplit(normalized).hostname
        for normalized in (normalize_url(export.site_url), normalize_url(export.home_url))
        if normalized
    }

    def add_reference(
        source_id: str,
        target_id: str,
        kind: str,
        field: str,
        strength: str,
        evidence: str,
    ) -> None:
        if not target_id or target_id not in reportable or source_id == target_id:
            return
        references.add(
            Reference(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                field=field,
                strength=strength,
                evidence=_compact_evidence(evidence),
            )
        )

    def add_url_reference(
        source_id: str,
        raw_url: str,
        kind: str,
        field: str,
        strength: str,
    ) -> set[str]:
        targets: set[str] = set()
        for key in media_variant_keys(raw_url, base_url):
            targets.update(url_map.get(key, set()))
        for target_id in targets:
            add_reference(source_id, target_id, kind, field, strength, raw_url)

        if not targets:
            normalized = normalize_url(raw_url, base_url)
            if normalized and urlsplit(normalized).hostname in site_hosts:
                unresolved_internal.add(normalized)
        return targets

    # Navigation items are trusted entry points.
    public_statuses = {"publish", "inherit"}
    menu_items = [item for item in export.items if item.post_type == "nav_menu_item"]
    for menu in menu_items:
        if menu.status and menu.status not in public_statuses:
            continue
        object_ids = menu.meta.get("_menu_item_object_id", [])
        custom_urls = menu.meta.get("_menu_item_url", [])
        for object_id in object_ids:
            if object_id and object_id != "0":
                add_reference(f"menu:{menu.id}", object_id, "menu", "_menu_item_object_id", "strong", object_id)
                if object_id in reportable:
                    roots.add(object_id)
        for custom_url in custom_urls:
            targets = add_url_reference(
                f"menu:{menu.id}", custom_url, "menu", "_menu_item_url", "strong"
            )
            roots.update(targets)

    # The item matching the exported home URL is also a trusted entry point.
    home_keys = url_keys(export.home_url or export.site_url, base_url)
    for key in home_keys:
        roots.update(url_map.get(key, set()))

    for item in reportable.values():
        if item.post_type in ARCHIVE_ENTRY_TYPES and item.status == "publish":
            roots.add(item.id)
            add_reference(
                f"archive:{item.post_type}",
                item.id,
                "archive",
                "post_type_archive",
                "strong",
                ARCHIVE_ENTRY_TYPES[item.post_type],
            )

    for item in reportable.values():
        if item.parent_id and item.parent_id != "0":
            add_reference(item.parent_id, item.id, "parent", "post_parent", "structural", item.parent_id)

        for thumbnail_id in item.meta.get("_thumbnail_id", []):
            add_reference(item.id, thumbnail_id, "featured-image", "_thumbnail_id", "strong", thumbnail_id)

        for field_name, body in (("content", item.content), ("excerpt", item.excerpt)):
            if not body:
                continue
            if len(body) > MAX_BODY_SCAN:
                export.warnings.append(f"Only the first {MAX_BODY_SCAN:,} characters of item {item.id} {field_name} were scanned.")
                body = body[:MAX_BODY_SCAN]
            parser = _ReferenceHTMLParser()
            try:
                parser.feed(body)
            except Exception:
                export.warnings.append(f"HTML reference parsing was incomplete for item {item.id}.")
            for raw_url, kind in parser.urls:
                add_url_reference(item.id, raw_url, kind, field_name, "strong")

            # Gutenberg comments and serialized fragments can contain URLs outside HTML attributes.
            for raw_url in URL_RE.findall(body):
                add_url_reference(item.id, raw_url.rstrip(".,);]"), "content-url", field_name, "strong")

            for match in WP_CLASS_ID_RE.finditer(body):
                add_reference(item.id, match.group(1), "content-embed", field_name, "strong", match.group(0))
            for match in GALLERY_RE.finditer(body):
                for target_id in NUMBER_RE.findall(match.group(1)):
                    add_reference(item.id, target_id, "gallery", field_name, "strong", match.group(0))
            for block in BLOCK_RE.finditer(body):
                block_json = block.group(1)
                for target_id in BLOCK_ID_RE.findall(block_json):
                    add_reference(item.id, target_id, "block-media", field_name, "strong", block.group(0))
                for id_list in BLOCK_IDS_RE.findall(block_json):
                    for target_id in NUMBER_RE.findall(id_list):
                        add_reference(item.id, target_id, "block-media", field_name, "strong", block.group(0))
            for block in REUSABLE_BLOCK_RE.finditer(body):
                for target_id in BLOCK_REF_RE.findall(block.group(1)):
                    add_reference(item.id, target_id, "reusable-block", field_name, "strong", block.group(0))
            for match in TABLEPRESS_RE.finditer(body):
                target_id = wordpress_id(match.group(1))
                if target_id:
                    add_reference(item.id, target_id, "tablepress", field_name, "strong", match.group(0))
            for match in GRAVITY_FORM_RE.finditer(body):
                target_id = wordpress_id(match.group(1))
                if target_id:
                    add_reference(item.id, target_id, "gravityform", field_name, "possible", match.group(0))
            for match in DOCUMENT_SHORTCODE_RE.finditer(body):
                target_id = wordpress_id(match.group(1))
                if target_id:
                    add_reference(item.id, target_id, "document-shortcode", field_name, "strong", match.group(0))

        # Unknown custom fields are useful evidence, but less trustworthy than rendered content.
        for meta_key, values in item.meta.items():
            if meta_key in {"_menu_item_url", "_menu_item_object_id", "_thumbnail_id", "_wp_attached_file"}:
                continue
            for value in values:
                if not value or len(value) > 2_000_000:
                    continue
                for raw_url in URL_RE.findall(value):
                    add_url_reference(item.id, raw_url.rstrip(".,);]"), "meta-url", meta_key, "possible")

    incoming: dict[str, list[Reference]] = defaultdict(list)
    outgoing: dict[str, list[Reference]] = defaultdict(list)
    for reference in references:
        incoming[reference.target_id].append(reference)
        if reference.source_id in reportable:
            outgoing[reference.source_id].append(reference)

    # Propagate reachability from menus/home through strong content references.
    reachable = set(roots)
    queue = deque(roots)
    while queue:
        source_id = queue.popleft()
        source = reportable.get(source_id)
        if source and source.status not in public_statuses:
            continue
        for reference in outgoing.get(source_id, []):
            if reference.strength != "strong" or reference.target_id in reachable:
                continue
            reachable.add(reference.target_id)
            queue.append(reference.target_id)

    rows: list[dict] = []
    classification_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()

    for item in reportable.values():
        item_class = _content_class(item)
        item_group = _content_group(item)
        is_media = item.post_type == "attachment"
        is_public = item.status in public_statuses
        inbound = incoming.get(item.id, [])
        strong_inbound = [reference for reference in inbound if reference.strength == "strong"]
        possible_inbound = [reference for reference in inbound if reference.strength == "possible"]
        structural_inbound = [reference for reference in inbound if reference.strength == "structural"]
        expected_author = _is_expected_author(item, expected_aliases)
        categories = sorted({term.name for term in item.terms if term.taxonomy == "category"})
        tags = sorted({term.name for term in item.terms if term.taxonomy in {"post_tag", "tag"}})
        taxonomies = sorted({f"{term.taxonomy}: {term.name}" for term in item.terms})
        taxonomy_terms: dict[str, list[str]] = defaultdict(list)
        taxonomy_slugs: dict[str, list[str]] = defaultdict(list)
        for term in item.terms:
            taxonomy_terms[term.taxonomy].append(term.name)
            if term.slug:
                taxonomy_slugs[term.taxonomy].append(term.slug)
        taxonomy_terms = {
            key: sorted(set(values)) for key, values in sorted(taxonomy_terms.items())
        }
        taxonomy_slugs = {
            key: sorted(set(values)) for key, values in sorted(taxonomy_slugs.items())
        }
        reasons: list[str] = []

        if not is_public:
            base_classification = "non-public"
            confidence = "high"
            recommendation = "Review separately from public orphan findings."
            reasons.append(f"The exported status is {item.status or 'unknown'}.")
        elif item.id in reachable:
            base_classification = "linked"
            archive_only = bool(strong_inbound) and all(reference.kind == "archive" for reference in strong_inbound)
            confidence = "medium" if archive_only else "high"
            recommendation = "Keep unless content review indicates otherwise."
            if archive_only:
                reasons.append("Assumed reachable from a public content archive. The export cannot prove that archive is enabled or lists this record.")
            else:
                reasons.append("Reachable from an exported menu, the site home URL, a public content archive, or linked reachable content.")
        elif is_media and strong_inbound:
            base_classification = "linked"
            confidence = "high"
            recommendation = "Keep unless every recorded use is removed."
            reasons.append(f"Found {len(strong_inbound)} strong exported reference(s) to this media item.")
        elif is_media and possible_inbound:
            base_classification = "needs-verification"
            confidence = "low"
            recommendation = "Verify custom-field and live-site use before deletion."
            reasons.append("Only possible references in custom metadata were found.")
        elif is_media:
            base_classification = "unreferenced-media"
            confidence = "medium" if structural_inbound else "high"
            recommendation = "Review as a deletion candidate; verify external and theme/plugin use first."
            reasons.append("No menu, content, embed, gallery, featured-image, or custom-field URL reference was found.")
        elif strong_inbound:
            base_classification = "disconnected"
            confidence = "medium"
            recommendation = "Review the disconnected source content and verify the live site."
            reasons.append(f"Found {len(strong_inbound)} strong inbound reference(s), but no path from an exported entry point.")
        elif possible_inbound:
            base_classification = "needs-verification"
            confidence = "low"
            recommendation = "Inspect the custom-field evidence and verify the live site."
            reasons.append("Only possible references in custom metadata were found.")
        else:
            base_classification = "unreferenced"
            confidence = "medium" if structural_inbound or categories else "high"
            recommendation = "Review as a deletion candidate; verify live and external use first."
            reasons.append("No strong inbound reference was found in the export.")

        if categories and item.post_type != "attachment" and item.id not in reachable:
            reasons.append("Taxonomy membership may make this item visible through an archive page.")
        if structural_inbound and not strong_inbound:
            reasons.append("A parent/child association exists, but it does not prove a rendered link.")

        classification = base_classification
        if expected_author and base_classification not in {"linked", "non-public"}:
            classification = "expected-development"
            reasons.insert(0, "The author matches the configured Greg Crouch development-content rule.")
            recommendation = "Confirm whether development is complete; retain or delete intentionally."

        evidence = []
        for reference in sorted(inbound, key=lambda ref: (ref.strength, ref.kind, ref.source_id)):
            source = by_id.get(reference.source_id)
            if source:
                source_title = source.title
            elif reference.kind == "archive":
                source_title = "Public content archive"
            elif reference.kind == "menu":
                source_title = "Navigation menu"
            else:
                source_title = "Site entry point"
            evidence.append(
                {
                    "source_id": reference.source_id,
                    "source_title": source_title,
                    "kind": reference.kind,
                    "field": reference.field,
                    "strength": reference.strength,
                    "evidence": reference.evidence,
                }
            )

        primary_url = item.attachment_url or item.url
        extension = PurePosixPath(urlsplit(primary_url).path).suffix.lower() if primary_url else ""
        attachment_details = _attachment_details(item)
        row = {
            "id": item.id,
            "type": item.post_type,
            "group": item_group,
            "group_label": GROUP_CONFIG[item_group]["label"],
            "content_class": item_class,
            "mime_type": item.mime_type,
            "file_extension": extension,
            "file_name": attachment_details.get("file_name", ""),
            "file_size": attachment_details.get("file_size"),
            "width": attachment_details.get("width"),
            "height": attachment_details.get("height"),
            "stored_path": attachment_details.get("stored_path", ""),
            "derivative_files": attachment_details.get("derivative_files", []),
            "derivative_count": attachment_details.get("derivative_count", 0),
            "alt_text": attachment_details.get("alt_text", ""),
            "caption": attachment_details.get("caption", ""),
            "description": attachment_details.get("description", ""),
            "title": item.title,
            "slug": item.slug,
            "url": primary_url,
            "wp_admin_url": wp_admin_edit_url(base_url, item.id),
            "status": item.status,
            "author_login": item.author_login,
            "author_name": item.author_name,
            "author_email": item.author_email,
            "created": item.created,
            "created_gmt": item.created_gmt,
            "modified": item.modified,
            "modified_gmt": item.modified_gmt,
            "parent_id": item.parent_id,
            "categories": categories,
            "tags": tags,
            "taxonomies": taxonomies,
            "taxonomy_terms": taxonomy_terms,
            "taxonomy_slugs": taxonomy_slugs,
            "meta_keys": sorted(item.meta),
            "classification": classification,
            "underlying_classification": base_classification,
            "confidence": confidence,
            "recommendation": recommendation,
            "reasons": reasons,
            "expected_development": expected_author,
            "reachable": item.id in reachable,
            "inbound_strong": len(strong_inbound),
            "inbound_possible": len(possible_inbound),
            "inbound_structural": len(structural_inbound),
            "outbound": len(outgoing.get(item.id, [])),
            "evidence": evidence,
        }
        rows.append(row)
        classification_counts[classification] += 1
        type_counts[item.post_type] += 1

    priority = {
        "unreferenced": 0,
        "unreferenced-media": 1,
        "disconnected": 2,
        "needs-verification": 3,
        "expected-development": 4,
        "non-public": 5,
        "linked": 6,
    }
    rows.sort(key=lambda row: (priority.get(row["classification"], 99), row["title"].casefold(), row["id"]))

    warnings = list(dict.fromkeys(export.warnings))
    if not menu_items:
        warnings.append("No navigation menu items were present; reachability classifications are less reliable.")
    warnings.append(
        "Export-only analysis cannot see hard-coded theme links, external links, every plugin field, or all live rendered behavior."
    )

    review_classes = {"unreferenced", "unreferenced-media", "disconnected", "needs-verification"}
    stats = {
        "exported_items": len(export.items),
        "reportable_items": len(rows),
        "public_items": sum(row["status"] in public_statuses for row in rows),
        "media_items": sum(row["type"] == "attachment" for row in rows),
        "review_candidates": sum(row["classification"] in review_classes for row in rows),
        "expected_development": classification_counts["expected-development"],
        "linked": classification_counts["linked"],
        "menu_items": len(menu_items),
        "references": len(references),
        "unresolved_internal_urls": len(unresolved_internal),
    }

    group_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        group_rows[row["group"]].append(row)

    taxonomy_catalog: dict[str, list[dict]] = {}
    groups: list[dict] = []
    for group_id, config in sorted(GROUP_CONFIG.items(), key=lambda pair: pair[1]["order"]):
        members = group_rows.get(group_id, [])
        if not members:
            continue
        status_counts = Counter(row["status"] or "unknown" for row in members)
        finding_counts = Counter(row["classification"] for row in members)
        subtype_counts = Counter(row["content_class"] for row in members)

        taxonomy_term_counts: dict[str, Counter[str]] = defaultdict(Counter)
        taxonomy_record_counts: Counter[str] = Counter()
        taxonomy_unique_keys: dict[str, set[str]] = defaultdict(set)
        for row in members:
            for taxonomy, terms in row["taxonomy_terms"].items():
                taxonomy_record_counts[taxonomy] += 1
                taxonomy_term_counts[taxonomy].update(terms)
                taxonomy_unique_keys[taxonomy].update(row.get("taxonomy_slugs", {}).get(taxonomy) or terms)

        taxonomies = []
        for taxonomy, term_counts in sorted(
            taxonomy_term_counts.items(),
            key=lambda pair: TAXONOMY_LABELS.get(pair[0], pair[0]).casefold(),
        ):
            taxonomies.append(
                {
                    "key": taxonomy,
                    "label": TAXONOMY_LABELS.get(taxonomy, taxonomy.replace("_", " ").title()),
                    "unique_terms": len(taxonomy_unique_keys[taxonomy]),
                    "assignments": sum(term_counts.values()),
                    "records_tagged": taxonomy_record_counts[taxonomy],
                    "terms": [
                        {"name": name, "count": count}
                        for name, count in sorted(
                            term_counts.items(), key=lambda pair: (-pair[1], pair[0].casefold())
                        )
                    ],
                }
            )
        taxonomy_catalog[group_id] = taxonomies

        group = {
            "id": group_id,
            "label": config["label"],
            "description": config["description"],
            "count": len(members),
            "public": sum(row["status"] in public_statuses for row in members),
            "review_candidates": sum(row["classification"] in review_classes for row in members),
            "expected_development": finding_counts["expected-development"],
            "linked": finding_counts["linked"],
            "status_counts": dict(sorted(status_counts.items())),
            "finding_counts": dict(sorted(finding_counts.items())),
            "subtype_counts": dict(sorted(subtype_counts.items())),
            "taxonomy_count": len(taxonomies),
            "incomplete": group_id in {"forms", "tablepress"},
            "note": (
                "The WXR contains form-associated posts and IDs, but not Gravity Forms definitions or field settings."
                if group_id == "forms"
                else "Table shortcodes are extracted when present, but plugin settings and unregistered table IDs may still be missing."
                if group_id == "tablepress"
                else ""
            ),
        }
        if group_id == "events":
            group["related_records"] = {
                "Venues": sum(item.post_type == "tribe_venue" for item in export.items),
                "Organizers": sum(item.post_type == "tribe_organizer" for item in export.items),
            }
        groups.append(group)

    source_coverage = [
        {"source": "WXR content and excerpts", "available": True, "detail": "Links, embeds, selected media blocks, galleries, TablePress/document shortcodes, and reusable-block refs"},
        {"source": "Navigation menus", "available": bool(menu_items), "detail": f"{len(menu_items)} exported menu records"},
        {"source": "Post custom fields", "available": True, "detail": "URL evidence and selected relationship IDs such as featured images"},
        {"source": "Attachment metadata", "available": True, "detail": "Paths, dimensions, ALT text, and generated variants when exported"},
        {"source": "Taxonomies", "available": True, "detail": "Typed terms and content assignments"},
        {"source": "Theme, widget, and site options", "available": False, "detail": "Not included in a standard WXR export"},
        {"source": "Gravity Forms definitions", "available": False, "detail": "Only associated post metadata is present"},
        {"source": "Live rendered pages", "available": False, "detail": "Requires an optional read-only crawl"},
        {"source": "Analytics and external links", "available": False, "detail": "Not represented in the export"},
    ]

    return {
        "site": {
            "title": export.site_title,
            "site_url": public_href(export.site_url),
            "home_url": public_href(export.home_url),
            "description": export.description,
        },
        "stats": stats,
        "classification_counts": dict(sorted(classification_counts.items())),
        "type_counts": dict(sorted(type_counts.items())),
        "groups": groups,
        "taxonomies_by_group": taxonomy_catalog,
        "taxonomy_labels": TAXONOMY_LABELS,
        "source_coverage": source_coverage,
        "authors": sorted({row["author_name"] or row["author_login"] for row in rows if row["author_name"] or row["author_login"]}),
        "categories": sorted({category for row in rows for category in row["categories"]}),
        "warnings": warnings,
        "items": rows,
    }
