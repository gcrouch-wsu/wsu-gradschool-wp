import json

import pytest
from werkzeug.security import generate_password_hash

from app_auth import load_users, normalize_email, verify_credentials


def test_auth_accepts_only_exact_wsu_addresses_and_hashed_passwords():
    password_hash = generate_password_hash("chosen password")
    users = load_users(json.dumps({"Reviewer@WSU.edu": password_hash}))
    assert list(users) == ["reviewer@wsu.edu"]
    assert normalize_email("reviewer@subdomain.wsu.edu") == ""
    assert verify_credentials(users, "REVIEWER@WSU.EDU", "chosen password", password_hash) == "reviewer@wsu.edu"
    assert verify_credentials(users, "reviewer@wsu.edu", "wrong", password_hash) == ""


@pytest.mark.parametrize("configured", [
    '{"reviewer@example.com":"scrypt:not-real"}',
    '{"reviewer@wsu.edu":"plaintext-password"}',
    '["reviewer@wsu.edu"]',
    '{not-json}',
])
def test_auth_configuration_fails_closed(configured):
    with pytest.raises(RuntimeError):
        load_users(configured)
