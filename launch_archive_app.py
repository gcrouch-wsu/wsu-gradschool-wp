from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
URL = "http://127.0.0.1:5055"


def ensure_dependencies() -> None:
    missing = [
        package
        for package in ("flask", "defusedxml")
        if importlib.util.find_spec(package) is None
    ]
    if not missing:
        return
    print(f"Installing local archive requirements for: {', '.join(missing)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])


def open_browser() -> None:
    time.sleep(1.2)
    webbrowser.open(URL)


def main() -> None:
    ensure_dependencies()
    from archive_app import app

    print("Starting WordPress Media Archive")
    print(f"Open {URL}")
    print("Leave this window open while files are being archived.")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)


if __name__ == "__main__":
    main()
