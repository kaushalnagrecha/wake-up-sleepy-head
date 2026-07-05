#!/usr/bin/env python3
"""
keep-alive.py - Health checker for Streamlit & HuggingFace Spaces.

Strategy:
  1. HTTP GET (requests) to fetch the page HTML - cheap pre-check.
  2. Inspect HTML for sleep/inactive markers.
  3. If asleep or inconclusive -> launch headless Selenium,
     wait for JS to render, click the wake button, and verify app content loads.
  4. If awake -> log and move on.

Endpoints are stored in a JSON dict keyed by platform - add or remove URLs as needed.
"""

import os
import sys
import time
import logging
import requests

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINTS: dict[str, list[str]] = {
    "streamlit": [
        "https://kn-f1-dashboard.streamlit.app/",
        "https://credit-access-in-uk-dashboard.streamlit.app",
        "https://aly6040-dashboard-kn.streamlit.app",
        "https://kn-dashboards-stockmarketanalysiscapitalizationdata.streamlit.app",
        "https://kn-top-5-companies-decision-board.streamlit.app",
    ],
    "huggingface": [
        "https://kaushal-nagrecha-ama-ai.hf.space",
    ],
}

# Timeouts (seconds)
HTTP_TIMEOUT = 30
STREAMLIT_PAGELOAD_TIMEOUT = 60       # Streamlit needs full JS execution
HF_PAGELOAD_TIMEOUT = 5              # HuggingFace - we use strategy "none"
SITE_WAIT_TIMEOUT = 60               # Total time to wait for sleep/awake detection
BUTTON_APPEAR_TIMEOUT = 20           # Time to wait for wake button after page load
WAKE_CONFIRM_TIMEOUT = 120           # Time to wait for app to come alive after clicking

# ---------------------------------------------------------------------------
# Sleep-detection markers (all lowercase for comparison)
# ---------------------------------------------------------------------------

STREAMLIT_SLEEP_MARKERS = [
    "yes, get this app back up!",
    "this app has gone to sleep due to inactivity",
    "zzzz",
]

# Markers that appear while the app is booting (after wake click, before app loads)
STREAMLIT_BOOTING_MARKERS = [
    "please wait",
    "waking up",
    "this app is booting",
    "starting up",
    "app is starting",
]

HUGGINGFACE_SLEEP_MARKERS = [
    "this space is sleeping due to inactivity",
    "restart this space",
    '"stage":"sleeping"',
    '"stage":"paused"',
]

# Streamlit wake button locators - XPATH text match is the most reliable
# because the button text hasn't changed across Streamlit versions.
STREAMLIT_WAKE_BUTTON_LOCATORS = [
    (By.XPATH, "//button[contains(text(),'Yes, get this app back up')]"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-viewer']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-owner']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button']"),
]

# HuggingFace restart button locators
HUGGINGFACE_RESTART_LOCATORS = [
    (By.CSS_SELECTOR, "form[action*='/start'] button[type='submit']"),
    (By.CSS_SELECTOR, "button.btn-lg"),
    (By.XPATH, "//button[contains(text(), 'Restart')]"),
]

# Streamlit app content selectors - presence of any means app is loaded
STREAMLIT_CONTENT_SELECTORS = [
    "[data-testid='stAppViewContainer']",
    "[data-testid='stSidebar']",
    "[data-testid='stHeader']",
    "section.main",
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
      True  -> definitely asleep
      False -> definitely awake
      None  -> inconclusive (need Selenium)

    NOTE: Streamlit sleeping apps return HTTP 200 with a static HTML shell.
    The sleep markers are rendered client-side by JS, so this pre-check will
    almost always return None for Streamlit. That's expected - Selenium
    handles the actual detection and wake-up.
    """
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        html = resp.text.lower()
    except requests.RequestException as exc:
        log.warning("  HTTP fetch failed: %s", exc)
        return None

    log.info("  HTTP %d - body length: %d chars", resp.status_code, len(resp.text))

    # Check sleep markers in raw HTML
    markers = get_sleep_markers(platform)
    sleep_hits = sum(1 for m in markers if m in html)
    if sleep_hits > 0:
        log.info("  Found %d sleep marker(s) in HTTP response - ASLEEP", sleep_hits)
        return True

    # For Streamlit: a small response body (~4KB) with no sleep markers
    # is almost certainly the sleeping HTML shell - return None (inconclusive)
    # rather than False, so Selenium can do the real check.
    if platform == "streamlit" and len(resp.text) < 10_000:
        log.info("  Small Streamlit response (%d chars) with no markers - likely sleeping shell, INCONCLUSIVE", len(resp.text))
        return None

    # Check for platform content markers in raw HTML
    content_sels = get_content_selectors(platform)
    for sel in content_sels:
        # Extract the meaningful part of the CSS selector for a substring search
        # e.g. "[data-testid='stAppViewContainer']" -> "stappviewcontainer"
        tag = sel.lower()
        for ch in "[].'\"#=":
            tag = tag.replace(ch, " ")
        # Use the longest token as the search key
        tokens = [t for t in tag.split() if len(t) > 3]
        if any(token in html for token in tokens):
            log.info("  Found content marker '%s' - AWAKE", sel)
            return False

    log.info("  No definitive markers in HTTP response - INCONCLUSIVE")
    return None


# ---------------------------------------------------------------------------
# Selenium driver
# ---------------------------------------------------------------------------

def create_driver(platform: str) -> webdriver.Chrome:
    """
    Headless Chrome.
    - Streamlit: normal page_load_strategy (needs full JS execution to render).
    - HuggingFace: page_load_strategy='none' (poll-based approach).
    """
    options = Options()

    # Use the Chrome binary from setup-chrome if available,
    # otherwise fall back to system default.
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    # KEY FIX: Streamlit's sleep page is a JS SPA - we MUST let JS execute.
    # Using strategy "none" + window.stop() kills the rendering pipeline.
    if platform == "huggingface":
        options.page_load_strategy = "none"
    # else: default ("normal") - waits for document load, JS executes fully

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


def app_content_loaded(driver, platform: str) -> bool:
    """Check if the actual app content has loaded (not the sleep or booting page)."""
    try:
        ready_state = driver.execute_script("return document.readyState") or ""
    except Exception:
        ready_state = ""

    if ready_state not in ("interactive", "complete"):
        return False

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
    except Exception:
        body_text = ""

    body_lower = body_text.lower()

    # Reject if sleep markers are still present
    markers = get_sleep_markers(platform)
    if any(m in body_lower for m in markers):
        return False

    # Reject if Streamlit booting markers are present (transitional screen)
    if platform == "streamlit":
        if any(m in body_lower for m in STREAMLIT_BOOTING_MARKERS):
            return False

    # For Streamlit: the actual app lives inside an iframe.
    # Check the top-level document first, then switch into any iframes.
    if platform == "streamlit":
        content_sels = get_content_selectors(platform)

        # Check top-level document
        try:
            if any(driver.find_elements(By.CSS_SELECTOR, sel) for sel in content_sels):
                return True
        except Exception:
            pass

        # Check inside iframes (Streamlit Community Cloud embeds the app in one)
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    driver.switch_to.frame(iframe)
                    if any(driver.find_elements(By.CSS_SELECTOR, sel) for sel in content_sels):
                        driver.switch_to.default_content()
                        return True
                    driver.switch_to.default_content()
                except Exception:
                    driver.switch_to.default_content()
        except Exception:
            pass

        return False

    # For HuggingFace: body text length or content selectors
    if len(body_text) >= 40:
        return True

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
# Streamlit-specific wake flow (WebDriverWait-based)
# ---------------------------------------------------------------------------

def wake_streamlit(url: str) -> bool:
    """
    Streamlit-specific wake flow:
      1. Load the page normally (let JS render the sleep UI).
      2. Use WebDriverWait to look for the wake button.
      3. If found -> click it, then wait for app content.
      4. If not found within timeout -> app is already awake.
    """
    driver = None
    try:
        driver = create_driver("streamlit")
        driver.set_page_load_timeout(STREAMLIT_PAGELOAD_TIMEOUT)

        log.info("  Selenium: Loading %s (normal strategy, letting JS render)", url)
        try:
            driver.get(url)
        except TimeoutException:
            log.warning("  Selenium: Page load timed out at %ds - continuing with partial load", STREAMLIT_PAGELOAD_TIMEOUT)

        # Wait a beat for any remaining JS to settle
        time.sleep(3)

        # Check: is the wake button present?
        log.info("  Selenium: Looking for wake button (up to %ds)...", BUTTON_APPEAR_TIMEOUT)
        wake_button = None
        try:
            # The primary locator - text match is the most reliable across versions
            wake_button = WebDriverWait(driver, BUTTON_APPEAR_TIMEOUT).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(text(),'Yes, get this app back up')]")
                )
            )
        except TimeoutException:
            # No wake button -> check if the app is already loaded
            pass

        if wake_button is None:
            # Try the other locators briefly
            for locator in STREAMLIT_WAKE_BUTTON_LOCATORS[1:]:
                try:
                    wake_button = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable(locator)
                    )
                    break
                except TimeoutException:
                    continue

        if wake_button is not None:
            log.info("  Selenium: Wake button found - app is ASLEEP. Clicking...")
            clicked = click_button_safe(driver, wake_button)
            if not clicked:
                log.warning("  Selenium: Failed to click wake button")
                return False

            log.info("  Selenium: Wake button clicked. Waiting up to %ds for app to boot...", WAKE_CONFIRM_TIMEOUT)

            # Wait for the button to disappear (indicates wake process started)
            try:
                WebDriverWait(driver, 15).until(
                    EC.invisibility_of_element_located(
                        (By.XPATH, "//button[contains(text(),'Yes, get this app back up')]")
                    )
                )
                log.info("  Selenium: Wake button disappeared - app is booting")
            except TimeoutException:
                log.warning("  Selenium: Wake button still visible after click - may not have registered")

            # Now wait for actual app content
            wake_deadline = time.time() + WAKE_CONFIRM_TIMEOUT
            while time.time() < wake_deadline:
                if app_content_loaded(driver, "streamlit"):
                    log.info("  Selenium: App is now AWAKE!")
                    return True
                time.sleep(3)

            # Click was sent - app may still be booting (cold starts can be slow)
            log.warning("  Selenium: Timed out waiting for content, but click was sent (app may still be booting)")
            return True

        else:
            # No wake button found - app should already be awake
            if app_content_loaded(driver, "streamlit"):
                log.info("  Selenium: App is already AWAKE (no wake button, content loaded)")
                return True
            else:
                # Edge case: neither button nor content. Maybe the page is still loading.
                log.info("  Selenium: No wake button found. Waiting a bit longer for content...")
                deadline = time.time() + 30
                while time.time() < deadline:
                    if app_content_loaded(driver, "streamlit"):
                        log.info("  Selenium: App is AWAKE (content appeared after extra wait)")
                        return True
                    time.sleep(2)
                log.warning("  Selenium: No wake button and no app content - unclear state")
                return False

    except Exception as exc:
        log.error("  Selenium error: %s", exc)
        return False
    finally:
        if driver:
            driver.quit()


# ---------------------------------------------------------------------------
# HuggingFace wake flow (poll-based, original logic)
# ---------------------------------------------------------------------------

def wake_huggingface(url: str) -> bool:
    """
    HuggingFace wake flow - uses page_load_strategy='none' and polling.
    """
    platform = "huggingface"
    driver = None
    try:
        driver = create_driver(platform)
        driver.set_page_load_timeout(HF_PAGELOAD_TIMEOUT)

        log.info("  Selenium: Loading %s (strategy=none)", url)
        try:
            driver.get(url)
        except (TimeoutException, WebDriverException):
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        log.info("  Selenium: Polling for up to %ds...", SITE_WAIT_TIMEOUT)
        deadline = time.time() + SITE_WAIT_TIMEOUT

        while time.time() < deadline:
            # Is it asleep?
            btn = find_wake_button(driver, platform)
            if btn is not None:
                log.info("  Selenium: Sleep detected - clicking restart button")
                clicked = click_button_safe(driver, btn)
                if not clicked:
                    log.warning("  Selenium: Restart button click failed")
                    return False

                log.info("  Selenium: Waiting up to %ds for app to boot...", WAKE_CONFIRM_TIMEOUT)
                wake_deadline = time.time() + WAKE_CONFIRM_TIMEOUT
                while time.time() < wake_deadline:
                    if app_content_loaded(driver, platform):
                        log.info("  Selenium: App is now AWAKE!")
                        return True
                    time.sleep(3)

                log.warning("  Selenium: Timed out after click (app may still be booting)")
                return True

            # Is it already awake?
            if app_content_loaded(driver, platform):
                log.info("  Selenium: App is already AWAKE")
                return True

            time.sleep(1)

        log.warning("  Selenium: Timed out - neither sleep markers nor app content detected")
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

def check_endpoint(url: str, platform: str) -> bool:
    """Check a single endpoint. Returns True if awake or successfully woken."""
    log.info("Checking: %s [platform=%s]", url, platform)

    # Step 1: Lightweight HTTP pre-check
    precheck = http_precheck(url, platform)

    if precheck is False:
        log.info("  RESULT: App is AWAKE (confirmed via HTTP)")
        return True

    if precheck is True:
        log.info("  App is ASLEEP (confirmed via HTTP) - launching Selenium")
    else:
        log.info("  Status inconclusive - launching Selenium to verify")

    # Step 2: Platform-specific Selenium wake-up
    if platform == "streamlit":
        return wake_streamlit(url)
    else:
        return wake_huggingface(url)


def main() -> int:
    total = sum(len(urls) for urls in ENDPOINTS.values())
    log.info("=" * 60)
    log.info("Keep-Alive Check - %d endpoint(s)", total)
    log.info("=" * 60)

    results: dict[str, bool] = {}

    for platform, urls in ENDPOINTS.items():
        for url in urls:
            success = check_endpoint(url, platform)
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
        log.warning("Some endpoints could not be woken - check logs above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
