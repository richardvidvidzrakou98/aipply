#!/usr/bin/env python3
"""One-time LinkedIn login helper.

Opens the bot's persistent Chrome profile so you can log into LinkedIn
manually (2FA, CAPTCHA, everything). The session is saved in
~/.aipply/chrome-profile and reused by all future runs.

Waits (up to --timeout seconds) for LinkedIn to redirect to your feed,
then closes and saves the session automatically. No keypress needed.
"""

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.linkedin_scanner import LinkedInScanner

LOGGED_IN_MARKERS = ["linkedin.com/feed", "/in/", "linkedin.com/mypreferences", "linkedin.com/notifications"]


def is_logged_in(url: str) -> bool:
    return any(marker in url for marker in LOGGED_IN_MARKERS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-time LinkedIn login")
    parser.add_argument("--timeout", type=int, default=240, help="Seconds to wait for login")
    args = parser.parse_args()

    scanner = LinkedInScanner()
    page = scanner.connect_browser()
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    time.sleep(2)

    if is_logged_in(page.url):
        print(f"✅ Already logged in (at {page.url})")
        scanner.close()
        sys.exit(0)

    print("\n✅ Browser window is open — log into LinkedIn there now.")
    print("   (Complete any CAPTCHA / 2FA in that window.)")
    print(f"   This script auto-detects login within {args.timeout}s and saves the session.\n")

    deadline = time.time() + args.timeout
    logged_in = False
    while time.time() < deadline:
        try:
            if is_logged_in(page.url):
                logged_in = True
                break
        except Exception:
            pass
        time.sleep(5)

    if logged_in:
        time.sleep(3)  # let the session fully settle
        print(f"✅ Login detected ({page.url}) — session saved.")
    else:
        print("⏰ Timed out waiting for login. Run this script again and try once more.")

    scanner.close()
    print("Done. You won't need to do this again — the session persists in ~/.aipply/chrome-profile")
