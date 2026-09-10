from __future__ import annotations

import json
from typing import Mapping

from werkzeug.security import check_password_hash, generate_password_hash


def normalize_email(value: object) -> str:
    email = str(value or "").strip().casefold()
    local, separator, domain = email.rpartition("@")
    if not separator or not local or domain != "wsu.edu":
        return ""
    return f"{local}@{domain}"


def load_users(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("APP_AUTH_USERS_JSON must be a JSON object of WSU emails and password hashes.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("APP_AUTH_USERS_JSON must be a JSON object.")
    users: dict[str, str] = {}
    for supplied_email, supplied_hash in parsed.items():
        email = normalize_email(supplied_email)
        password_hash = str(supplied_hash or "").strip()
        if not email:
            raise RuntimeError("Every configured application user must have an exact @wsu.edu email address.")
        if not password_hash.startswith(("scrypt:", "pbkdf2:")):
            raise RuntimeError("Application passwords must be Werkzeug password hashes, not plaintext.")
        if email in users:
            raise RuntimeError(f"Duplicate application user: {email}.")
        users[email] = password_hash
    return users


def verify_credentials(users: Mapping[str, str], email: object, password: object, dummy_hash: str) -> str:
    normalized = normalize_email(email)
    supplied_password = str(password or "")
    stored_hash = users.get(normalized, dummy_hash)
    try:
        valid = check_password_hash(stored_hash, supplied_password)
    except (TypeError, ValueError):
        valid = False
    return normalized if normalized in users and valid else ""


def make_dummy_hash() -> str:
    return generate_password_hash("not-a-configured-user-password")
