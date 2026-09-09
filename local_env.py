from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_local_env() -> None:
    """Load .env then .env.local for local runs. Never load these on Vercel."""
    if os.environ.get("VERCEL"):
        return
    for name in (".env", ".env.local"):
        path = ROOT / name
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("'\"")


def rest_credentials_configured() -> bool:
    return bool(
        os.environ.get("WP_REST_BASE_URL", "").strip()
        and os.environ.get("WP_REST_USERNAME", "").strip()
        and os.environ.get("WP_REST_APPLICATION_PASSWORD", "").strip()
        and os.environ.get("WP_REST_ENABLED", "").strip().lower() in {"1", "true", "yes"}
    )


def rest_enabled(testing: bool = False) -> bool:
    if os.environ.get("VERCEL"):
        return False
    if testing and os.environ.get("WP_REST_ALLOW_IN_TESTS") != "1":
        return False
    return rest_credentials_configured()


def rest_write_enabled(testing: bool = False) -> bool:
    if not rest_enabled(testing=testing):
        return False
    return os.environ.get("WP_REST_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes"}
