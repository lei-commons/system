"""CI check: the image can start its headless Chromium as its own non-root user (no network needed)."""

from playwright.sync_api import sync_playwright


def main() -> int:
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
        print("chromium", browser.version)
        browser.close()
    finally:
        pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
