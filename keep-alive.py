#!/usr/bin/env python3
"""
keep-alive.py — Lightweight health checker for Streamlit & HuggingFace Spaces.

Strategy:
  1. HTTP GET (requests) to fetch the page HTML.
  2. Inspect HTML for sleep/inactive markers.
  3. If asleep or inconclusive → launch headless Selenium to click the wake button.
  4. If awake → log and move on.

Endpoints are stored in a simple list — add or remove URLs as needed.
"""

import sys
import time
import logging
import subprocess
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINTS: list[str] = [
    "https://kaushal-nagrecha-ama-ai.hf.space",
    "https://kn-f1-dashboard.streamlit.app/",
]

# Maximum seconds to wait for Selenium wake-up confirmation
SELENIUM_WAKE_TIMEOUT = 120

# Seconds between Selenium polls while waiting for wake
SELENIUM_POLL_INTERVAL = 5

# HTTP request timeout (seconds)
HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Sleep-detection markers
# ---------------------------------------------------------------------------

STREAMLIT_SLEEP_MARKERS = [
    "Zzzz",
    "This app has gone to sleep due to inactivity",
    "Yes, get this app back up!",
    "app_woke_up",
    "If this is your app",
]

HUGGINGFACE_SLEEP_MARKERS = [
    "This Space is sleeping due to inactivity",
    "Restart this Space",
    '"stage":"SLEEPING"',
    '"stage":"PAUSED"',
    '"stage":"BUILDING"',
    "is paused",
]

AWAKE_MARKERS = [
    "streamlit-wide",
    "stApp",
    "#root",
    "gradio-app",
    "gradio",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("keep-alive")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_endpoint(url: str) -> str:
    """Return 'streamlit' or 'huggingface' based on URL pattern."""
    if "streamlit" in url:
        return "streamlit"
    if "hf.space" in url or "huggingface.co" in url:
        return "huggingface"
    return "unknown"


def fetch_page(url: str) -> tuple[int, str]:
    """
    Fetch page content via HTTP GET.
    Returns (status_code, body_text). On failure returns (-1, "").
    """
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        return resp.status_code, resp.text
    except requests.RequestException as exc:
        log.warning("HTTP fetch failed for %s: %s", url, exc)
        return -1, ""


def is_asleep(html: str, platform: str) -> bool | None:
    """
    Determine if the page is asleep.
    Returns True (asleep), False (awake), or None (inconclusive).
    """
    markers = (
        STREAMLIT_SLEEP_MARKERS
        if platform == "streamlit"
        else HUGGINGFACE_SLEEP_MARKERS
    )

    html_lower = html.lower()

    # Check for sleep markers
    sleep_hits = sum(1 for m in markers if m.lower() in html_lower)
    if sleep_hits > 0:
        log.info("  → Found %d sleep marker(s) — page is ASLEEP", sleep_hits)
        return True

    # Check for awake markers
    awake_hits = sum(1 for m in AWAKE_MARKERS if m.lower() in html_lower)
    if awake_hits > 0:
        log.info("  → Found %d awake marker(s) — page is AWAKE", awake_hits)
        return False

    # If the body is almost empty or very short, inconclusive
    if len(html.strip()) < 200:
        log.info("  → Response body too short (%d chars) — INCONCLUSIVE", len(html.strip()))
        return None

    log.info("  → No definitive markers found — INCONCLUSIVE")
    return None


# ---------------------------------------------------------------------------
# Selenium wake-up
# ---------------------------------------------------------------------------

def wake_with_selenium(url: str, platform: str) -> bool:
    """
    Launch headless Chrome via Selenium and click the wake/restart button.
    Returns True if the app appears to have woken up.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        log.error("Selenium is not installed — cannot perform browser wake-up.")
        log.error("Install with: pip install selenium")
        return False

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1280,900")

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        log.info("  → Selenium: Loading %s", url)
        driver.get(url)
        time.sleep(3)  # Let JS render

        clicked = False

        if platform == "huggingface":
            clicked = _click_hf_restart(driver)
        elif platform == "streamlit":
            clicked = _click_streamlit_wake(driver)

        if not clicked:
            log.warning("  → Selenium: Could not find or click wake button")
            return False

        # Poll until the page shows awake markers
        log.info("  → Selenium: Waiting up to %ds for app to wake...", SELENIUM_WAKE_TIMEOUT)
        start = time.time()
        while time.time() - start < SELENIUM_WAKE_TIMEOUT:
            time.sleep(SELENIUM_POLL_INTERVAL)
            page_src = driver.page_source
            status = is_asleep(page_src, platform)
            if status is False:
                log.info("  → Selenium: App is now AWAKE!")
                return True

        log.warning("  → Selenium: Timed out waiting for wake-up")
        return False

    except Exception as exc:
        log.error("  → Selenium error: %s", exc)
        return False
    finally:
        if driver:
            driver.quit()


def _click_hf_restart(driver) -> bool:
    """Click the HuggingFace 'Restart this Space' button."""
    from selenium.webdriver.common.by import By

    # HuggingFace wraps the restart button in a <form> with a submit button
    selectors = [
        "form[action*='/start'] button[type='submit']",
        "button.btn-lg",
        "//button[contains(text(), 'Restart')]",
    ]

    for sel in selectors[:2]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            log.info("  → Selenium: Found HF restart button via CSS '%s'", sel)
            btn.click()
            return True
        except Exception:
            continue

    # Fallback: XPath
    try:
        btn = driver.find_element(By.XPATH, selectors[2])
        log.info("  → Selenium: Found HF restart button via XPath")
        btn.click()
        return True
    except Exception:
        pass

    return False


def _click_streamlit_wake(driver) -> bool:
    """Click the Streamlit 'Yes, get this app back up!' button."""
    from selenium.webdriver.common.by import By

    selectors = [
        "//button[contains(text(), 'Yes, get this app back up')]",
        "//button[contains(text(), 'get this app back up')]",
        "//a[contains(text(), 'Yes, get this app back up')]",
    ]

    for xpath in selectors:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            log.info("  → Selenium: Found Streamlit wake button via XPath")
            btn.click()
            return True
        except Exception:
            continue

    # Fallback: any prominent button
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for b in buttons:
            if "wake" in b.text.lower() or "back up" in b.text.lower():
                log.info("  → Selenium: Found wake button by text scan: '%s'", b.text)
                b.click()
                return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def check_endpoint(url: str) -> bool:
    """
    Check a single endpoint. Returns True if awake (or successfully woken).
    """
    platform = classify_endpoint(url)
    log.info("Checking: %s [platform=%s]", url, platform)

    status_code, html = fetch_page(url)

    if status_code == -1:
        log.warning("  → HTTP request failed — attempting Selenium wake-up")
        return wake_with_selenium(url, platform)

    log.info("  → HTTP %d — body length: %d chars", status_code, len(html))

    sleep_status = is_asleep(html, platform)

    if sleep_status is False:
        log.info("  → ✓ App is AWAKE — no action needed")
        return True

    if sleep_status is True:
        log.info("  → ✗ App is ASLEEP — launching Selenium to wake it")
        return wake_with_selenium(url, platform)

    # Inconclusive
    log.info("  → ? Status inconclusive — launching Selenium as precaution")
    return wake_with_selenium(url, platform)


def main() -> int:
    log.info("=" * 60)
    log.info("Keep-Alive Check — %d endpoint(s)", len(ENDPOINTS))
    log.info("=" * 60)

    results: dict[str, bool] = {}

    for url in ENDPOINTS:
        success = check_endpoint(url)
        results[url] = success
        log.info("")

    # Summary
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    all_ok = True
    for url, ok in results.items():
        icon = "✓" if ok else "✗"
        log.info("  %s %s", icon, url)
        if not ok:
            all_ok = False

    if all_ok:
        log.info("All endpoints are alive!")
        return 0
    else:
        log.warning("Some endpoints could not be woken — check logs above")
        return 1


if __name__ == "__main__":
    sys.exit(main())