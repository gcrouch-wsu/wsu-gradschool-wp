from io import BytesIO

from analyzer import analyze_export, parse_wxr
from analyzer.url_normalizer import url_keys


WXR = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
 xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <title>Test Graduate School</title>
  <link>https://example.test</link>
  <wp:base_site_url>https://example.test</wp:base_site_url>
  <wp:author>
    <wp:author_login>gcrouch</wp:author_login>
    <wp:author_email>greg@example.test</wp:author_email>
    <wp:author_display_name>Greg Crouch</wp:author_display_name>
  </wp:author>
  <wp:author>
    <wp:author_login>editor</wp:author_login>
    <wp:author_display_name>Site Editor</wp:author_display_name>
  </wp:author>
  <item>
    <title>Menu link</title><link>https://example.test/menu-link</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded>
    <wp:post_id>90</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified>
    <wp:status>publish</wp:status><wp:post_type>nav_menu_item</wp:post_type>
    <wp:postmeta><wp:meta_key>_menu_item_object_id</wp:meta_key><wp:meta_value>1</wp:meta_value></wp:postmeta>
  </item>
  <item>
    <title>Landing page</title><link>https://example.test/landing</link>
    <dc:creator>editor</dc:creator>
    <content:encoded><![CDATA[<a href="/child/">Child</a><img class="wp-image-4" src="https://cdn.test/photo-300x200.jpg">]]></content:encoded>
    <excerpt:encoded></excerpt:encoded><wp:post_id>1</wp:post_id>
    <wp:post_date>2026-01-01 00:00:00</wp:post_date><wp:post_modified>2026-01-02 00:00:00</wp:post_modified>
    <wp:status>publish</wp:status><wp:post_type>page</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
  <item>
    <title>Child page</title><link>https://example.test/child</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>2</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>page</wp:post_type><wp:post_parent>1</wp:post_parent>
  </item>
  <item>
    <title>Development page</title><link>https://example.test/dev</link>
    <dc:creator>gcrouch</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>3</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>page</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
  <item>
    <title>Photo</title><link>https://example.test/?attachment_id=4</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>4</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>inherit</wp:status>
    <wp:post_type>attachment</wp:post_type><wp:post_parent>1</wp:post_parent>
    <wp:attachment_url>https://cdn.test/photo.jpg</wp:attachment_url>
  </item>
  <item>
    <title>Past colloquium</title><link>https://example.test/event/colloquium</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>20</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>tribe_events</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
</channel></rss>"""


def _rows_by_id():
    export = parse_wxr(BytesIO(WXR))
    report = analyze_export(export)
    return report, {row["id"]: row for row in report["items"]}


def test_parser_resolves_author_and_infers_attachment_mime_type():
    export = parse_wxr(BytesIO(WXR))
    development = next(item for item in export.items if item.id == "3")
    photo = next(item for item in export.items if item.id == "4")
    assert development.author_name == "Greg Crouch"
    assert development.author_email == "greg@example.test"
    assert photo.mime_type == "image/jpeg"


def test_menu_and_content_links_propagate_reachability():
    report, rows = _rows_by_id()
    assert rows["1"]["classification"] == "linked"
    assert rows["2"]["classification"] == "linked"
    assert rows["4"]["classification"] == "linked"
    assert report["stats"]["menu_items"] == 1


def test_expected_development_rule_keeps_underlying_finding():
    _, rows = _rows_by_id()
    assert rows["3"]["classification"] == "expected-development"
    assert rows["3"]["underlying_classification"] == "unreferenced"


def test_query_urls_do_not_collapse_to_the_same_root_path():
    first = url_keys("https://example.test/?attachment_id=4")
    second = url_keys("https://example.test/?attachment_id=5")
    assert first.isdisjoint(second)


def test_report_exposes_wordpress_groups_and_typed_taxonomies():
    report, rows = _rows_by_id()
    groups = {group["id"]: group for group in report["groups"]}
    assert groups["pages"]["count"] == 3
    assert groups["media"]["count"] == 1
    assert rows["4"]["group"] == "media"
    assert "taxonomy_labels" in report
    assert rows["1"]["wp_admin_url"] == "https://example.test/wp-admin/post.php?post=1&action=edit"


def test_published_events_are_reachable_from_the_calendar_archive():
    _, rows = _rows_by_id()
    assert rows["20"]["group"] == "events"
    assert rows["20"]["classification"] == "linked"
    assert rows["20"]["confidence"] == "medium"
    assert rows["20"]["evidence"][0]["kind"] == "archive"


def test_tablepress_shortcode_links_the_table_record():
    wxr = WXR.replace(
        b"<a href=\"/child/\">Child</a>",
        b"<a href=\"/child/\">Child</a>[table id=99]",
    ).replace(
        b"</channel></rss>",
        b"""  <item>
    <title>Program table</title><link>https://example.test/table/99</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>99</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>tablepress_table</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
</channel></rss>""",
    )
    report = analyze_export(parse_wxr(BytesIO(wxr)))
    rows = {row["id"]: row for row in report["items"]}
    assert rows["99"]["group"] == "tablepress"
    assert rows["99"]["classification"] == "linked"
    assert any(entry["kind"] == "tablepress" for entry in rows["99"]["evidence"])
    groups = {group["id"]: group for group in report["groups"]}
    assert groups["tablepress"]["incomplete"] is True


def test_report_surfaces_warnings_and_taxonomy_slugs():
    wxr = WXR.replace(
        b"<wp:post_parent>0</wp:post_parent>\n  </item>\n  <item>\n    <title>Child page</title>",
        b"""<wp:post_parent>0</wp:post_parent>
    <category domain="category" nicename="news"><![CDATA[News]]></category>
  </item>
  <item>
    <title>Child page</title>""",
    )
    report = analyze_export(parse_wxr(BytesIO(wxr)))
    rows = {row["id"]: row for row in report["items"]}
    assert rows["1"]["taxonomy_slugs"]["category"] == ["news"]
    assert rows["1"]["taxonomy_terms"]["category"] == ["News"]
    assert any("cannot see hard-coded theme links" in warning for warning in report["warnings"])
    pages = next(group for group in report["groups"] if group["id"] == "pages")
    catalog = next(taxonomy for taxonomy in report["taxonomies_by_group"]["pages"] if taxonomy["key"] == "category")
    assert catalog["unique_terms"] == 1
    assert catalog["terms"][0]["name"] == "News"
    assert pages["taxonomy_count"] == 1


def test_shortcodes_and_reusable_blocks_create_references():
    wxr = WXR.replace(
        b"<a href=\"/child/\">Child</a>",
        b"""<a href="/child/">Child</a><!-- wp:block {"ref":55} /-->[gravityform id=7][document id=8]""",
    ).replace(
        b"</channel></rss>",
        b"""  <item>
    <title>Shared block</title><link>https://example.test/block/55</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>55</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>wp_block</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
  <item>
    <title>Policy PDF</title><link>https://example.test/document/8</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>8</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
  <item>
    <title>Contact form</title><link>https://example.test/form/7</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>7</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>wsuwp_form</wp:post_type><wp:post_parent>0</wp:post_parent>
    <wp:postmeta><wp:meta_key>_gform-form-id</wp:meta_key><wp:meta_value>7</wp:meta_value></wp:postmeta>
  </item>
</channel></rss>""",
    )
    report = analyze_export(parse_wxr(BytesIO(wxr)))
    rows = {row["id"]: row for row in report["items"]}
    assert rows["55"]["classification"] == "linked"
    assert any(entry["kind"] == "reusable-block" for entry in rows["55"]["evidence"])
    assert rows["8"]["group"] == "documents"
    assert rows["8"]["classification"] == "linked"
    assert any(entry["kind"] == "document-shortcode" for entry in rows["8"]["evidence"])
    assert rows["7"]["group"] == "forms"
    assert any(entry["kind"] == "gravityform" for entry in rows["7"]["evidence"])


def test_parser_skips_non_canonical_wordpress_ids():
    hostile = WXR.replace(b"<wp:post_id>3</wp:post_id>", b"<wp:post_id>3?force=true</wp:post_id>")
    export = parse_wxr(BytesIO(hostile))
    assert all(item.id != "3?force=true" for item in export.items)
    assert all(item.id != "3" or item.title != "Development page" for item in export.items)
