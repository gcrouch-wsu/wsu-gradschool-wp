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
    assert rows["20"]["evidence"][0]["kind"] == "archive"


def test_parser_skips_non_canonical_wordpress_ids():
    hostile = WXR.replace(b"<wp:post_id>3</wp:post_id>", b"<wp:post_id>3?force=true</wp:post_id>")
    export = parse_wxr(BytesIO(hostile))
    assert all(item.id != "3?force=true" for item in export.items)
    assert all(item.id != "3" or item.title != "Development page" for item in export.items)
