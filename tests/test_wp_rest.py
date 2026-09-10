import pytest

from analyzer.ids import rest_base_url_allowed, same_http_origin, sites_are_same, wordpress_id
from analyzer.url_normalizer import normalize_url, public_href
from analyzer.wp_rest import WordPressRestClient, live_check_items, trash_items, wp_admin_edit_url


def test_wp_admin_edit_url():
    assert wp_admin_edit_url("https://gradschool.wsu.edu/", "13") == (
        "https://gradschool.wsu.edu/wp-admin/post.php?post=13&action=edit"
    )


def test_list_type_follows_total_pages_header():
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(url)
        if "&page=2" in url or url.endswith("page=2"):
            return 200, [{"id": 2, "status": "publish", "type": "page"}], {"x-wp-total": "2", "x-wp-totalpages": "2"}
        return 200, [{"id": 1, "status": "publish", "type": "page"}], {"x-wp-total": "2", "x-wp-totalpages": "2"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    status, payload = client.list_type("pages", ["1", "2"])
    assert status == 200
    assert {record["id"] for record in payload} == {1, 2}
    assert any("&page=2" in url or url.endswith("page=2") for url in seen)


def test_list_type_rejects_unrelated_include_records():
    def fake_get(url, headers, timeout):
        return 200, [{"id": 99, "status": "publish", "type": "page"}], {"x-wp-total": "1", "x-wp-totalpages": "1"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    status, payload = client.list_type("pages", ["1"])
    assert status == 502
    assert "include list" in payload["message"]


def test_list_type_rejects_non_list_payload():
    def fake_get(url, headers, timeout):
        return 200, {"message": "not a list"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    status, payload = client.list_type("pages", ["1"])
    assert status == 502
    results = live_check_items([{"id": "1", "type": "page"}], client)
    assert results["1"]["live_state"] == "error"
    assert "unexpected" in results["1"]["error"]


def test_list_type_rejects_success_without_completeness_headers():
    def fake_get(url, headers, timeout):
        return 200, [{"id": 1, "status": "publish", "type": "page"}]

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    status, payload = client.list_type("pages", ["1", "2"])
    assert status == 502
    assert "pagination totals" in payload["message"]


def test_list_type_errors_when_pagination_exceeds_limit():
    def fake_get(url, headers, timeout):
        return 200, [{"id": 1, "status": "publish", "type": "page"}], {"x-wp-total": "1", "x-wp-totalpages": "21"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    status, payload = client.list_type("pages", ["1"])
    assert status == 502
    assert "pagination" in payload["message"]


def test_list_type_sends_csv_include_list():
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(url)
        return 200, [], {"x-wp-total": "0", "x-wp-totalpages": "0"}

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
            }], {"x-wp-total": "1", "x-wp-totalpages": "1"}
        if "/tablepress_table?" in url:
            return 404, {"code": "rest_no_route"}
        return 200, [], {"x-wp-total": "0", "x-wp-totalpages": "0"}

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
        }], {"x-wp-total": "1", "x-wp-totalpages": "1"}

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
    assert not sites_are_same(["https://gradschool.wsu.edu"], "https://www.gradschool.wsu.edu")
    assert not sites_are_same(["https://example.test"], "https://gradschool.wsu.edu")
    assert not sites_are_same(["https://gradschool.wsu.edu:8443"], "https://gradschool.wsu.edu")
    assert not sites_are_same(["https://example.test/site-a"], "https://example.test/site-b")
    assert sites_are_same(["https://example.test/site-a"], "https://example.test/site-a/")
    assert not sites_are_same(["http://example.test"], "https://example.test")
    assert same_http_origin("https://example.test/wp-json/wp/v2/pages/1", "https://example.test/wp-json/wp/v2/pages/2")
    assert not same_http_origin("https://example.test/wp-json/wp/v2/pages/1", "https://evil.test/wp-json/wp/v2/pages/1")
    assert normalize_url("https://example.test:notaport/path") == ""


def test_client_rejects_plaintext_remote_rest():
    with pytest.raises(ValueError):
        WordPressRestClient("http://gradschool.wsu.edu", "gcrouch", "secret")


def test_trash_rejects_mismatched_delete_identity():
    def fake_get(url, headers, timeout):
        return 200, {"id": 3, "status": "publish", "type": "page", "title": {"rendered": "Development page"}}

    def fake_delete(url, headers, timeout):
        return 200, {"id": 99, "status": "trash", "type": "page"}

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    results = trash_items([{"id": "3", "type": "page", "title": "Development page"}], client)
    assert results[0]["ok"] is False
    assert "identity" in results[0]["error"]


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


def test_trash_refuses_when_live_record_changed_after_check():
    deleted = []

    def fake_get(url, headers, timeout):
        return 200, {
            "id": 3,
            "status": "publish",
            "type": "page",
            "title": {"rendered": "Development page"},
            "link": "https://example.test/dev",
            "modified": "2026-01-03T00:00:00",
        }

    def fake_delete(url, headers, timeout):
        deleted.append(url)
        return 200, {"id": 3, "status": "trash", "type": "page"}

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    snapshot = {"3": {
        "live_state": "found",
        "live_status": "publish",
        "live_type": "page",
        "live_title": "Development page",
        "live_link": "https://example.test/dev",
        "live_modified": "2026-01-02T00:00:00",
        "checked_at": "2026-01-02T00:00:00+00:00",
    }}
    results = trash_items(
        [{"id": "3", "type": "page", "title": "Development page"}],
        client,
        expected_live=snapshot,
    )
    assert results[0]["ok"] is False
    assert "changed" in results[0]["error"]
    assert deleted == []


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


def test_trash_reports_remaining_items_when_wordpress_refuses_auth():
    def fake_get(url, headers, timeout):
        return 401, {"message": "rest_forbidden"}

    client = WordPressRestClient("https://example.test", "gcrouch", "secret", http_get=fake_get)
    results = trash_items(
        [
            {"id": "3", "type": "page", "title": "Development page"},
            {"id": "1", "type": "page", "title": "Landing page"},
        ],
        client,
    )
    assert len(results) == 2
    assert all(not result["ok"] for result in results)


def test_trash_confirms_state_after_an_uncertain_delete_response():
    reads = 0
    deletes = 0

    def fake_get(url, headers, timeout):
        nonlocal reads
        reads += 1
        return 200, {
            "id": 3,
            "status": "publish" if reads == 1 else "trash",
            "type": "page",
            "title": {"rendered": "Development page"},
            "link": "https://example.test/dev",
            "modified": "2026-01-02T00:00:00",
        }

    def fake_delete(url, headers, timeout):
        nonlocal deletes
        deletes += 1
        raise TimeoutError("response timed out")

    client = WordPressRestClient(
        "https://example.test", "gcrouch", "secret", http_get=fake_get, http_delete=fake_delete
    )
    results = trash_items(
        [{"id": "3", "type": "page", "title": "Development page"}],
        client,
        site_url="https://example.test",
    )
    assert deletes == 1
    assert reads == 2
    assert results[0]["ok"] is True
    assert results[0]["live"]["live_state"] == "trashed"
    assert "uncertain" in results[0]["notice"]
