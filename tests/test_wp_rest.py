import pytest

from analyzer.ids import rest_base_url_allowed, sites_are_same, wordpress_id
from analyzer.url_normalizer import public_href
from analyzer.wp_rest import WordPressRestClient, live_check_items, trash_items, wp_admin_edit_url


def test_wp_admin_edit_url():
    assert wp_admin_edit_url("https://gradschool.wsu.edu/", "13") == (
        "https://gradschool.wsu.edu/wp-admin/post.php?post=13&action=edit"
    )


def test_list_type_sends_csv_include_list():
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(url)
        return 200, []

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    client.list_type("pages", ["1", "2", "3"])
    assert len(seen) == 1
    assert "include=1%2C2%2C3" in seen[0]
    assert "status=any%2Ctrash" in seen[0]
    assert seen[0].count("include=") == 1


def test_live_check_found_missing_and_unregistered():
    def fake_get(url, headers, timeout):
        assert headers["Authorization"].startswith("Basic ")
        if "/pages?" in url:
            return 200, [{
                "id": 1,
                "status": "publish",
                "link": "https://example.test/landing",
                "modified": "2026-01-02T00:00:00",
                "type": "page",
            }]
        if "/tablepress_table?" in url:
            return 404, {"code": "rest_no_route"}
        return 200, []

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    results = live_check_items(
        [
            {"id": "1", "type": "page"},
            {"id": "3", "type": "page"},
            {"id": "9", "type": "tablepress_table"},
        ],
        client,
    )
    assert results["1"]["live_state"] == "found"
    assert results["1"]["live_status"] == "publish"
    assert results["3"]["live_state"] == "missing"
    assert results["9"]["live_state"] == "not-in-rest"


def test_live_check_labels_trashed_records():
    def fake_get(url, headers, timeout):
        assert "status=any%2Ctrash" in url
        return 200, [{
            "id": 1311,
            "status": "trash",
            "link": "https://gradschool.wsu.edu/?page_id=1311",
            "modified": "2026-01-02T00:00:00",
            "type": "page",
        }]

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    results = live_check_items(
        [{"id": "1311", "type": "page"}],
        client,
        site_url="https://gradschool.wsu.edu",
    )
    assert results["1311"]["live_state"] == "trashed"
    assert results["1311"]["live_found"] is True
    assert results["1311"]["wp_admin_url"] == (
        "https://gradschool.wsu.edu/wp-admin/edit.php?post_status=trash&post_type=page"
    )


def test_wordpress_ids_reject_query_injection():
    assert wordpress_id("123") == "123"
    assert wordpress_id("123?force=true") is None
    assert wordpress_id("0") is None
    assert public_href("javascript:alert(1)") == ""
    assert rest_base_url_allowed("https://gradschool.wsu.edu")
    assert not rest_base_url_allowed("http://gradschool.wsu.edu")
    assert rest_base_url_allowed("http://127.0.0.1")
    assert sites_are_same(["https://gradschool.wsu.edu"], "https://www.gradschool.wsu.edu")
    assert not sites_are_same(["https://example.test"], "https://gradschool.wsu.edu")


def test_client_rejects_plaintext_remote_rest():
    with pytest.raises(ValueError):
        WordPressRestClient("http://gradschool.wsu.edu", "gcrouch", "secret")


def test_trash_uses_delete_without_force():
    seen = []

    def fake_get(url, headers, timeout):
        return 200, {
            "id": 3,
            "status": "publish",
            "type": "page",
            "title": {"rendered": "Development page"},
        }

    def fake_delete(url, headers, timeout):
        seen.append(url)
        return 200, {
            "id": 3,
            "status": "trash",
            "type": "page",
            "link": "https://example.test/dev",
            "modified": "2026-01-02T00:00:00",
        }

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    results = trash_items(
        [{"id": "3", "type": "page", "title": "Development page"}],
        client,
        site_url="https://example.test",
    )
    assert seen == ["https://example.test/wp-json/wp/v2/pages/3"]
    assert "force" not in seen[0]
    assert "?" not in seen[0].rsplit("/", 1)[-1]
    assert results[0]["ok"] is True
    assert results[0]["live"]["live_state"] == "trashed"
    assert results[0]["live"]["wp_admin_url"] == (
        "https://example.test/wp-admin/edit.php?post_status=trash&post_type=page"
    )


def test_trash_never_sends_hostile_ids():
    seen = []

    def fake_delete(url, headers, timeout):
        seen.append(url)
        return 200, {"id": 3, "status": "trash", "type": "page"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_delete=fake_delete)
    results = trash_items([{"id": "123?force=true", "type": "page", "title": "Hostile"}], client)
    assert seen == []
    assert results[0]["ok"] is False


def test_trash_refuses_when_live_type_does_not_match_export():
    def fake_get(url, headers, timeout):
        return 200, {"id": 3, "status": "publish", "type": "post"}

    def fake_delete(url, headers, timeout):
        raise AssertionError("Trash must not run when the live type does not match.")

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    results = trash_items([{"id": "3", "type": "page", "title": "Development page"}], client)
    assert results[0]["ok"] is False
    assert "does not match" in results[0]["error"]


def test_trash_refuses_media_when_wordpress_requires_force_delete():
    def fake_get(url, headers, timeout):
        return 200, {"id": 4, "status": "inherit", "type": "attachment"}

    def fake_delete(url, headers, timeout):
        return 410, {"code": "rest_cannot_delete", "message": "Force delete is required because media does not support trashing."}

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    results = trash_items([{"id": "4", "type": "attachment", "title": "Photo"}], client)
    assert results[0]["ok"] is False
    assert "will not permanently delete" in results[0]["error"]
