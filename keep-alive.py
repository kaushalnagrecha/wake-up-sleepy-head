#!/usr/bin/env python3
"""
keep-alive.py — Health checker for Streamlit & HuggingFace Spaces.

Strategy:
  1. HTTP GET (requests) to fetch the page HTML — cheap pre-check.
  2. Inspect HTML for sleep/inactive markers.
  3. If asleep or inconclusive → launch headless Selenium with page_load_strategy="none",
     poll for sleep markers, click the wake button, and verify app content loads.
  4. If awake → log and move on.

Endpoints are stored in a simple list — add or remove URLs as needed.
"""

import sys
import time
import logging
import requests

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINTS: list[str] = [
    "https://kaushal-nagrecha-ama-ai.hf.space",
    "https://kn-f1-dashboard.streamlit.app/",
]

# Timeouts (seconds)
HTTP_TIMEOUT = 30
BROWSER_PAGELOAD_TIMEOUT = 5          # Intentionally low — we use strategy "none"
SITE_WAIT_TIMEOUT = 60                # Total time to wait for sleep/awake detection
BUTTON_APPEAR_TIMEOUT = 15            # Time to wait for wake button after sleep detected
WAKE_CONFIRM_TIMEOUT = 120            # Time to wait for app to come alive after clicking

# ---------------------------------------------------------------------------
# Sleep-detection markers (all lowercase for comparison)
# ---------------------------------------------------------------------------

STREAMLIT_SLEEP_MARKERS = [
    "yes, get this app back up!",
    "this app has gone to sleep due to inactivity",
    "zzzz",
]

HUGGINGFACE_SLEEP_MARKERS = [
    "this space is sleeping due to inactivity",
    "restart this space",
    '"stage":"sleeping"',
    '"stage":"paused"',
]

# Streamlit wake button locators — data-testid selectors first (most reliable)
STREAMLIT_WAKE_BUTTON_LOCATORS = [
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-viewer']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-owner']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button']"),
    (By.XPATH, "//button[normalize-space()='Yes, get this app back up!']"),
]

# HuggingFace restart button locators
HUGGINGFACE_RESTART_LOCATORS = [
    (By.CSS_SELECTOR, "form[action*='/start'] button[type='submit']"),
    (By.CSS_SELECTOR, "button.btn-lg"),
    (By.XPATH, "//button[contains(text(), 'Restart')]"),
]

# Streamlit app content selectors — presence of any means app is loaded
STREAMLIT_CONTENT_SELECTORS = [
    "[data-testid='stAppViewContainer']",
    "[data-testid='stSidebar']",
    "[data-testid='stHeader']",
    "section.main",
    "main",
]

# HuggingFace app content selectors
HUGGINGFACE_CONTENT_SELECTORS = [
    "gradio-app",
    ".gradio-container",
    "#root",
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
# Platform helpers
# ---------------------------------------------------------------------------

def classify_endpoint(url: str) -> str:
    if "streamlit" in url:
        return "streamlit"
    if "hf.space" in url or "huggingface.co" in url:
        return "huggingface"
    return "unknown"


def get_sleep_markers(platform: str) -> list[str]:
    if platform == "streamlit":
        return STREAMLIT_SLEEP_MARKERS
    return HUGGINGFACE_SLEEP_MARKERS


def get_wake_locators(platform: str) -> list[tuple]:
    if platform == "streamlit":
        return STREAMLIT_WAKE_BUTTON_LOCATORS
    return HUGGINGFACE_RESTART_LOCATORS


def get_content_selectors(platform: str) -> list[str]:
    if platform == "streamlit":
        return STREAMLIT_CONTENT_SELECTORS
    return HUGGINGFACE_CONTENT_SELECTORS


# ---------------------------------------------------------------------------
# HTTP pre-check
# ---------------------------------------------------------------------------

def http_precheck(url: str, platform: str) -> bool | None:
    """
    Lightweight HTTP check. Returns:
      True  → definitely asleep
      False → definitely awake
      None  → inconclusive (need Selenium)
    """
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        html = resp.text.lower()
    except requests.RequestException as exc:
        log.warning("  HTTP fetch failed: %s", exc)
        return None

    log.info("  HTTP %d — body length: %d chars", resp.status_code, len(resp.text))

    # Check sleep markers
    markers = get_sleep_markers(platform)
    sleep_hits = sum(1 for m in markers if m in html)
    if sleep_hits > 0:
        log.info("  Found %d sleep marker(s) in HTTP response — ASLEEP", sleep_hits)
        return True

    # Check content markers (lowercase match in raw HTML)
    content_sels = get_content_selectors(platform)
    for sel in content_sels:
        if sel.lower().strip("[].'\"#") in html:
            log.info("  Found content marker '%s' — AWAKE", sel)
            return False

    log.info("  No definitive markers in HTTP response — INCONCLUSIVE")
    return None


# ---------------------------------------------------------------------------
# Selenium driver
# ---------------------------------------------------------------------------

def create_driver() -> webdriver.Chrome:
    """Headless Chrome with page_load_strategy='none' — don't block on full load."""
    options = Options()
    options.page_load_strategy = "none"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--window-size=1280,900")
    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Selenium detection helpers
# ---------------------------------------------------------------------------

def find_wake_button(driver, platform: str):
    """Find a visible, enabled wake/restart button. Returns element or None."""
    locators = get_wake_locators(platform)
    for locator in locators:
        try:
            for button in driver.find_elements(*locator):
                if button.is_displayed() and button.is_enabled():
                    return button
        except Exception:
            continue
    return None


def sleep_marker_present(driver, platform: str) -> bool:
    """Check if sleep markers are visible in the live DOM."""
    # Direct button check first — most reliable signal
    if find_wake_button(driver, platform) is not None:
        return True

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        body_text = ""

    markers = get_sleep_markers(platform)
    return any(m in body_text for m in markers)


def app_content_loaded(driver, platform: str) -> bool:
    """Check if the actual app content has loaded (not the sleep page)."""
    try:
        ready_state = driver.execute_script("return document.readyState") or ""
    except Exception:
        ready_state = ""

    if ready_state not in ("interactive", "complete"):
        return False

    # Make sure sleep markers are gone
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
    except Exception:
        body_text = ""

    markers = get_sleep_markers(platform)
    if any(m in body_text.lower() for m in markers):
        return False

    # Substantial body text means the app rendered
    if len(body_text) >= 40:
        return True

    # Check for platform-specific content selectors in the DOM
    content_sels = get_content_selectors(platform)
    try:
        return any(
            driver.find_elements(By.CSS_SELECTOR, sel)
            for sel in content_sels
        )
    except Exception:
        return False


def click_button_safe(driver, button) -> bool:
    """Click with scrollIntoView + JS fallback."""
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", button
        )
    except Exception:
        pass

    # Try native click first
    try:
        button.click()
        return True
    except Exception:
        pass

    # JS fallback
    try:
        driver.execute_script("arguments[0].click();", button)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Selenium wake-up (core logic)
# ---------------------------------------------------------------------------

def wake_with_selenium(url: str, platform: str) -> bool:
    """
    Launch headless Chrome, detect sleep state, click wake button, verify.
    Returns True if the app is awake or was successfully woken.
    """
    driver = None
    try:
        driver = create_driver()
        driver.set_page_load_timeout(BROWSER_PAGELOAD_TIMEOUT)

        log.info("  Selenium: Loading %s (strategy=none)", url)
        try:
            driver.get(url)
        except (TimeoutException, WebDriverException):
            # Expected with strategy "none" + low timeout — stop loading gracefully
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        # Phase 1: Poll until we detect sleep markers OR app content
        log.info("  Selenium: Polling for up to %ds...", SITE_WAIT_TIMEOUT)
        deadline = time.time() + SITE_WAIT_TIMEOUT

        while time.time() < deadline:
            # Is it asleep?
            if sleep_marker_present(driver, platform):
                log.info("  Selenium: Sleep markers detected — looking for wake button")

                # Wait for the wake button to become clickable
                btn_deadline = time.time() + BUTTON_APPEAR_TIMEOUT
                clicked = False

                while time.time() < btn_deadline:
                    btn = find_wake_button(driver, platform)
                    if btn is not None:
                        log.info("  Selenium: Found wake button — clicking")
                        clicked = click_button_safe(driver, btn)
                        if clicked:
                            log.info("  Selenium: Wake button clicked successfully")
                        break
                    time.sleep(1)

                if not clicked:
                    log.warning("  Selenium: Wake button never appeared or click failed")
                    return False

                # Phase 2: Wait for the app to actually come alive
                log.info("  Selenium: Waiting up to %ds for app to boot...", WAKE_CONFIRM_TIMEOUT)
                wake_deadline = time.time() + WAKE_CONFIRM_TIMEOUT

                while time.time() < wake_deadline:
                    if app_content_loaded(driver, platform):
                        log.info("  Selenium: App is now AWAKE!")
                        return True
                    time.sleep(3)

                # Click was sent — app may still be booting
                log.warning("  Selenium: Timed out waiting for content after click (app may still be booting)")
                return True

            # Is it already awake?
            if app_content_loaded(driver, platform):
                log.info("  Selenium: App is already AWAKE")
                return True

            time.sleep(1)

        log.warning("  Selenium: Timed out — neither sleep markers nor app content detected")
        return False

    except Exception as exc:
        log.error("  Selenium error: %s", exc)
        return False
    finally:
        if driver:
            driver.quit()


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def check_endpoint(url: str) -> bool:
    """Check a single endpoint. Returns True if awake or successfully woken."""
    platform = classify_endpoint(url)
    log.info("Checking: %s [platform=%s]", url, platform)

    # Step 1: Lightweight HTTP pre-check
    precheck = http_precheck(url, platform)

    if precheck is False:
        log.info("  RESULT: App is AWAKE (confirmed via HTTP)")
        return True

    if precheck is True:
        log.info("  App is ASLEEP (confirmed via HTTP) — launching Selenium")
    else:
        log.info("  Status inconclusive — launching Selenium to verify")

    # Step 2: Selenium wake-up
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
