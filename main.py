#!/usr/bin/env python3
"""
Instagram Unfollow Bot
======================
Unfollow accounts that don't follow you back, either by scraping
your profile or from a pre-built text file of usernames.

Usage examples:
    # Interactive — just run it and follow the prompts
    python insta_bot.py

    # Unfollow from a file
    python insta_bot.py -u your_username -f not_following_back.txt

    # Resume from where you left off
    python insta_bot.py -u your_username -f list.txt --start-index 50

    # Scrape followers/following and auto-unfollow
    python insta_bot.py -u your_username --scrape

    # Use Firefox instead of Chrome
    python insta_bot.py -u your_username -f list.txt --browser firefox

    # Slower, safer delays
    python insta_bot.py -u your_username -f list.txt --min-delay 45 --max-delay 90

Environment variables (optional):
    INSTAGRAM_USERNAME   Your Instagram username
    INSTAGRAM_PASSWORD   Your password (only needed for --auto-login)
"""

import os
import random
import logging
from dataclasses import dataclass
from time import sleep
from datetime import datetime
from typing import Set, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

# ─── Logging ────────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.FileHandler("insta_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    """All tuneable settings in one place."""

    username: str
    password: Optional[str] = None
    manual_login: bool = True
    headless: bool = False
    browser: str = "chrome"          # "chrome" or "firefox"
    max_unfollows: int = 50
    min_delay: float = 30.0          # seconds between unfollows
    max_delay: float = 60.0
    start_index: int = 0
    file: Optional[str] = None       # path to username list
    scrape: bool = False             # scrape followers/following live
    verbose: bool = False
    login_timeout: int = 300         # seconds to wait for manual login
    page_load_timeout: int = 120
    element_wait: int = 20

    def __post_init__(self):
        if self.manual_login and self.headless:
            logger.warning("Manual login is incompatible with headless mode — disabling headless.")
            self.headless = False
        if not self.manual_login and not self.password:
            raise ValueError(
                "Password is required for automated login. "
                "Either set INSTAGRAM_PASSWORD or use manual login."
            )


# ─── CSS / XPath selectors (updated 2024) ───────────────────────────────────────

class Selectors:
    # Login
    USERNAME  = "input[name='username']"
    PASSWORD  = "input[name='password']"
    SUBMIT    = "button[type='submit']"

    # Popups
    NOT_NOW_XPATHS = [
        "//button[contains(text(), 'Not Now')]",
        "//div[contains(text(), 'Not Now')]",
        "//button[contains(text(), 'Not now')]",
        "//button[contains(text(), 'Cancel')]",
    ]

    # Profile
    FOLLOWERS_LINK  = "//a[contains(@href, '/followers')]"
    FOLLOWING_LINK  = "//a[contains(@href, '/following')]"

    # Dialog user list
    DIALOG_USER_LINKS = "div[role='dialog'] a[role='link']"


# ─── Human-like behaviour helpers ───────────────────────────────────────────────

def _rand_sleep(lo: float = 1.0, hi: float = 3.0) -> None:
    sleep(random.uniform(lo, hi))


def _human_type(element, text: str) -> None:
    for ch in text:
        element.send_keys(ch)
        sleep(random.uniform(0.05, 0.2))


def _human_scroll(driver) -> None:
    driver.execute_script(f"window.scrollBy(0, {random.randint(200, 500)});")
    _rand_sleep(0.5, 1.5)


def _random_mouse(driver, element) -> None:
    try:
        actions = ActionChains(driver)
        actions.move_by_offset(random.randint(-100, 100), random.randint(-100, 100)).perform()
        _rand_sleep(0.1, 0.3)
        actions.move_to_element(element).perform()
        _rand_sleep(0.1, 0.3)
    except Exception:
        ActionChains(driver).reset_actions()


# ─── Anti-detection ─────────────────────────────────────────────────────────────

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
const _origQ = window.navigator.permissions.query;
window.navigator.permissions.query = p =>
    p.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : _origQ(p);
"""

_UA_FIREFOX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)
_UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _make_firefox_options(headless: bool) -> FirefoxOptions:
    opts = FirefoxOptions()
    opts.add_argument("--width=1366")
    opts.add_argument("--height=768")
    opts.set_preference("dom.webdriver.enabled", False)
    opts.set_preference("useAutomationExtension", False)
    opts.set_preference("marionette.enabled", False)
    opts.set_preference("general.useragent.override", _UA_FIREFOX)
    opts.set_preference("media.peerconnection.enabled", False)
    opts.set_preference("dom.webnotifications.enabled", False)
    opts.set_preference("dom.push.enabled", False)
    if headless:
        opts.add_argument("--headless")
    return opts


def _make_chrome_options(headless: bool) -> ChromeOptions:
    opts = ChromeOptions()
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={_UA_CHROME}")
    for flag in ("--disable-notifications", "--disable-infobars",
                 "--disable-extensions", "--disable-gpu",
                 "--no-sandbox", "--disable-dev-shm-usage"):
        opts.add_argument(flag)
    if headless:
        opts.add_argument("--headless=new")
    return opts


# ─── Main bot class ─────────────────────────────────────────────────────────────

class InstaBot:
    """Instagram bot for unfollowing non-followers."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._init_browser()

    # ── Browser setup ───────────────────────────────────────────────────────

    def _init_browser(self) -> None:
        cfg = self.cfg
        if cfg.browser == "firefox":
            self.driver = webdriver.Firefox(options=_make_firefox_options(cfg.headless))
        else:
            self.driver = webdriver.Chrome(options=_make_chrome_options(cfg.headless))

        self.driver.set_page_load_timeout(cfg.page_load_timeout)
        self.driver.set_script_timeout(cfg.page_load_timeout)
        self.driver.implicitly_wait(10)
        self.wait = WebDriverWait(self.driver, cfg.element_wait)

        # Inject stealth on blank page first
        self.driver.get("about:blank")
        self.driver.execute_script(_STEALTH_JS)
        logger.info("Browser ready (%s, headless=%s)", cfg.browser, cfg.headless)

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self.driver:
            self.driver.quit()
            logger.info("Browser closed")

    # ── Low-level helpers ───────────────────────────────────────────────────

    def _find(self, by: By, selector: str, timeout: int = 15, clickable: bool = False):
        cond = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
        return WebDriverWait(self.driver, timeout).until(cond((by, selector)))

    def _click(self, el, js: bool = False) -> bool:
        try:
            _random_mouse(self.driver, el)
            _rand_sleep(0.3, 0.7)
            if js:
                self.driver.execute_script("arguments[0].click();", el)
            else:
                el.click()
            return True
        except ElementClickInterceptedException:
            try:
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except Exception as e:
                logger.error("JS click fallback failed: %s", e)
                return False
        except Exception as e:
            logger.error("Click failed: %s", e)
            return False

    def _dismiss_popups(self) -> None:
        for xpath in Selectors.NOT_NOW_XPATHS:
            try:
                btn = WebDriverWait(self.driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                self._click(btn)
                _rand_sleep(1, 2)
                logger.info("Dismissed popup")
            except (TimeoutException, Exception):
                continue

    # ── Login ───────────────────────────────────────────────────────────────

    def login(self) -> bool:
        logger.info("Navigating to Instagram…")
        self.driver.get("https://www.instagram.com/")
        _rand_sleep(3, 5)
        self.driver.execute_script(_STEALTH_JS)

        if self.cfg.manual_login:
            return self._manual_login()
        return self._auto_login()

    def _auto_login(self) -> bool:
        try:
            logger.info("Entering credentials…")
            user_el = self._find(By.CSS_SELECTOR, Selectors.USERNAME, clickable=True)
            self._click(user_el)
            _rand_sleep(0.5, 1)
            _human_type(user_el, self.cfg.username)

            _rand_sleep(0.5, 1.5)
            pw_el = self._find(By.CSS_SELECTOR, Selectors.PASSWORD)
            self._click(pw_el)
            _rand_sleep(0.5, 1)
            _human_type(pw_el, self.cfg.password)

            _rand_sleep(1, 2)
            self._click(self._find(By.CSS_SELECTOR, Selectors.SUBMIT, clickable=True))
            _rand_sleep(5, 8)
            self._dismiss_popups()

            if "accounts/login" not in self.driver.current_url:
                logger.info("Login successful")
                return True
            logger.error("Still on login page — login may have failed")
            return False
        except Exception as e:
            logger.error("Login failed: %s", e)
            return False

    def _manual_login(self) -> bool:
        timeout = self.cfg.login_timeout
        print(f"\n{'='*60}")
        print("🔐  MANUAL LOGIN REQUIRED")
        print(f"{'='*60}")
        print("Log in to Instagram in the browser window.")
        print("Complete any security challenges if prompted.")
        print(f"Waiting up to {timeout // 60} minutes…")
        print(f"{'='*60}\n")

        start = datetime.now()
        while (datetime.now() - start).seconds < timeout:
            try:
                url = self.driver.current_url
                if "instagram.com" in url and "accounts/" not in url:
                    # Check for logged-in indicators
                    for label in ("Home", "New post"):
                        try:
                            self.driver.find_element(By.CSS_SELECTOR, f"svg[aria-label='{label}']")
                            print("\n✅  Login detected! Continuing…\n")
                            logger.info("Manual login succeeded")
                            _rand_sleep(2, 3)
                            self._dismiss_popups()
                            return True
                        except NoSuchElementException:
                            continue
                    # Fallback: URL-based check
                    if "/accounts/" not in url:
                        print("\n✅  Login detected (URL)! Continuing…\n")
                        _rand_sleep(2, 3)
                        self._dismiss_popups()
                        return True
            except Exception:
                pass

            sleep(3)
            elapsed = (datetime.now() - start).seconds
            if elapsed > 0 and elapsed % 30 == 0:
                remaining = timeout - elapsed
                print(f"⏳  Still waiting… {remaining // 60}m {remaining % 60}s left")

        print("\n❌  Login timeout.")
        logger.error("Manual login timed out")
        return False

    # ── Profile navigation ──────────────────────────────────────────────────

    def _go_to_profile(self) -> bool:
        try:
            self.driver.get(f"https://www.instagram.com/{self.cfg.username}/")
            _rand_sleep(3, 5)
            return True
        except Exception as e:
            logger.error("Navigate to profile failed: %s", e)
            return False

    # ── Followers / Following ───────────────────────────────────────────────

    @staticmethod
    def _parse_count(text: str) -> int:
        text = text.strip().replace(",", "").replace(" ", "")
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        if text and text[-1].upper() in multipliers:
            return int(float(text[:-1]) * multipliers[text[-1].upper()])
        return int(text)

    def _scroll_dialog(self, max_scrolls: int = 100) -> None:
        logger.info("Scrolling dialog to load all users…")
        js = """
        const d = document.querySelector('div[role="dialog"] div[style*="overflow"]');
        if (d) { d.scrollTop = d.scrollHeight; return d.scrollHeight; }
        return 0;
        """
        prev, stale = 0, 0
        for i in range(1, max_scrolls + 1):
            cur = self.driver.execute_script(js)
            stale = stale + 1 if cur == prev else 0
            if stale >= 3:
                break
            prev = cur
            _rand_sleep(0.8, 2.0)
            if i % 10 == 0:
                logger.info("  scrolled %d times…", i)
        logger.info("Finished scrolling")

    def _extract_usernames_from_dialog(self) -> Set[str]:
        skip = {"", "explore", "reels", "stories"}
        usernames: Set[str] = set()
        for el in self.driver.find_elements(By.CSS_SELECTOR, Selectors.DIALOG_USER_LINKS):
            try:
                href = el.get_attribute("href") or ""
                name = href.rstrip("/").split("/")[-1]
                if name not in skip:
                    usernames.add(name)
            except StaleElementReferenceException:
                continue
        logger.info("Extracted %d usernames", len(usernames))
        return usernames

    def _close_dialog(self) -> None:
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            _rand_sleep(1, 2)
        except Exception as e:
            logger.warning("Close dialog failed: %s", e)

    def _get_user_list(self, link_xpath: str) -> Tuple[Set[str], int]:
        link = self._find(By.XPATH, link_xpath, clickable=True)
        try:
            count = self._parse_count(link.find_element(By.CSS_SELECTOR, "span span").text)
        except Exception:
            count = 0
        self._click(link)
        _rand_sleep(2, 4)
        self._scroll_dialog()
        names = self._extract_usernames_from_dialog()
        self._close_dialog()
        return names, count

    def get_followers(self) -> Tuple[Set[str], int]:
        logger.info("Getting followers…")
        return self._get_user_list(Selectors.FOLLOWERS_LINK)

    def get_following(self) -> Tuple[Set[str], int]:
        logger.info("Getting following…")
        return self._get_user_list(Selectors.FOLLOWING_LINK)

    # ── Unfollow logic ──────────────────────────────────────────────────────

    def _unfollow_user(self, username: str) -> bool:
        verbose = self.cfg.verbose
        try:
            if verbose:
                print(f"  → Navigating to {username}…")
            self.driver.get(f"https://www.instagram.com/{username}/")
            _rand_sleep(2, 4)

            # Step 1: find the "Following" button
            btn = self._find_following_button(verbose)
            if not btn:
                return False

            if verbose:
                print("  → Clicking 'Following'…")
            self._click(btn)
            _rand_sleep(1, 2)

            # Step 2: confirm "Unfollow" in the popup
            confirm = self._find_unfollow_confirm(verbose)
            if not confirm:
                ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                return False

            if verbose:
                print("  → Clicking 'Unfollow'…")
            self._click(confirm, js=True)
            _rand_sleep(1, 2)
            logger.info("Unfollowed: %s", username)
            return True

        except TimeoutException:
            logger.warning("Timeout unfollowing %s", username)
            return False
        except Exception as e:
            logger.error("Failed to unfollow %s: %s", username, e)
            return False

    def _find_following_button(self, verbose: bool):
        xpaths = [
            "//button[contains(., 'Following')]",
            "//div[@role='button'][contains(., 'Following')]",
            "//*[text()='Following']//ancestor::button | //*[text()='Following']//ancestor::div[@role='button']",
        ]
        for i, xp in enumerate(xpaths, 1):
            try:
                el = self._find(By.XPATH, xp, timeout=5 if i == 1 else 3, clickable=True)
                if verbose:
                    print(f"  → Found via method {i}")
                return el
            except TimeoutException:
                continue
        logger.warning("'Following' button not found")
        if verbose:
            self._debug_buttons()
        return None

    def _find_unfollow_confirm(self, verbose: bool):
        xpaths = [
            "//div[@role='dialog']//button[text()='Unfollow'] | //div[@role='dialog']//*[text()='Unfollow']",
            "//*[text()='Unfollow']",
            "//*[contains(translate(text(), 'UNFOLLOW', 'unfollow'), 'unfollow')]",
        ]
        for i, xp in enumerate(xpaths, 1):
            try:
                el = self._find(By.XPATH, xp, timeout=5 if i == 1 else 3, clickable=True)
                if verbose:
                    print(f"  → Found 'Unfollow' via method {i}")
                return el
            except TimeoutException:
                continue
        logger.warning("'Unfollow' confirmation not found")
        return None

    def _debug_buttons(self) -> None:
        buttons = self.driver.find_elements(By.TAG_NAME, "button")
        print(f"  → {len(buttons)} buttons on page. First 5:")
        for i, b in enumerate(buttons[:5]):
            try:
                print(f"     [{i}] {b.text[:50]}")
            except Exception:
                pass

    # ── Batch unfollow ──────────────────────────────────────────────────────

    def _unfollow_batch(self, usernames: Set[str]) -> Tuple[int, Set[str], int]:
        cfg = self.cfg
        ordered = sorted(usernames)
        total = len(ordered)
        unfollowed: Set[str] = set()
        failed: Set[str] = set()
        count = 0

        print(f"\n📋  {total} accounts to process")
        print(f"🎯  Limit: {cfg.max_unfollows} per session")
        print(f"⏱️   Delay: {cfg.min_delay}–{cfg.max_delay}s between unfollows")
        if cfg.verbose:
            print("🔍  Verbose: ON")
        print()

        last_idx = cfg.start_index
        for i, name in enumerate(ordered[cfg.start_index:], start=cfg.start_index):
            last_idx = i
            if count >= cfg.max_unfollows:
                print(f"\n⚠️   Reached limit of {cfg.max_unfollows}. Resume with --start-index {i}")
                break

            print(f"[{count + 1}/{cfg.max_unfollows}] {name}  ({i + 1}/{total})")

            if count > 0:
                delay = random.uniform(cfg.min_delay, cfg.max_delay)
                print(f"  ⏳ Waiting {delay:.0f}s…")
                sleep(delay)

            if self._unfollow_user(name):
                unfollowed.add(name)
                count += 1
                print("  ✅ Done")
            else:
                failed.add(name)
                print("  ❌ Failed")

        # Summary
        print(f"\n{'='*50}")
        print(f"📊  Session summary")
        print(f"{'='*50}")
        print(f"✅  Unfollowed: {len(unfollowed)}")
        print(f"❌  Failed:     {len(failed)}")
        print(f"📍  Last index: {last_idx}")
        if failed:
            preview = ", ".join(sorted(failed)[:10])
            extra = f" … and {len(failed) - 10} more" if len(failed) > 10 else ""
            print(f"\nFailed: {preview}{extra}")

        return count, unfollowed, last_idx

    # ── File I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_file(path: str) -> Set[str]:
        names: Set[str] = set()
        try:
            with open(path) as f:
                for line in f:
                    name = line.strip()
                    if name and not name.startswith("#") and name != "Not_Following_You_Back":
                        names.add(name)
            logger.info("Loaded %d usernames from %s", len(names), path)
        except FileNotFoundError:
            logger.error("File not found: %s", path)
        return names

    @staticmethod
    def _save_unfollowed(names: Set[str], path: str = "accounts_unfollowed.txt") -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a") as f:
            f.write(f"\n--- Session: {ts} ---\n")
            for n in sorted(names):
                f.write(f"{n}\n")
        logger.info("Saved %d unfollowed accounts to %s", len(names), path)

    # ── High-level entry points ─────────────────────────────────────────────

    def run_from_file(self) -> None:
        if not self.login():
            logger.error("Login failed — exiting.")
            return

        print("✅  Login successful — starting unfollow process…")
        _rand_sleep(3, 5)

        # Confirm browser is alive
        try:
            _ = self.driver.current_url
        except Exception:
            print("\n❌  Browser connection lost. Try again.")
            return

        names = self._load_file(self.cfg.file)
        if not names:
            logger.error("No usernames loaded.")
            return

        _, unfollowed, _ = self._unfollow_batch(names)
        if unfollowed:
            self._save_unfollowed(unfollowed)

    def run_scrape(self) -> None:
        if not self.login():
            logger.error("Login failed — exiting.")
            return

        _rand_sleep(3, 5)

        if not self._go_to_profile():
            return
        _rand_sleep(2, 4)
        followers, _ = self.get_followers()

        _rand_sleep(3, 5)
        if not self._go_to_profile():
            return
        _rand_sleep(2, 4)
        following, _ = self.get_following()

        non_followers = following - followers
        logger.info("Found %d non-followers", len(non_followers))

        _, unfollowed, _ = self._unfollow_batch(non_followers)
        if unfollowed:
            self._save_unfollowed(unfollowed)

    def run(self) -> None:
        """Dispatch to the right mode based on config."""
        if self.cfg.file and not self.cfg.scrape:
            self.run_from_file()
        else:
            self.run_scrape()


# ─── Interactive setup (runs when no args provided) ─────────────────────────────

def _interactive_setup() -> BotConfig:
    """Walk the user through setup with prompts."""
    print()
    print("=" * 60)
    print("  Instagram Unfollow Bot — Interactive Setup")
    print("=" * 60)
    print()

    # Username
    username = input("Instagram username: ").strip()
    if not username:
        print("❌  Username cannot be empty.")
        raise SystemExit(1)

    # Mode
    print()
    print("How do you want to unfollow?")
    print("  1) From a text file of usernames (recommended)")
    print("  2) Scrape my followers/following and auto-detect")
    print()
    mode = input("Choose [1/2] (default: 1): ").strip() or "1"

    file_path = None
    scrape = False
    start_index = 0

    if mode == "2":
        scrape = True
    else:
        file_path = input("Path to username file: ").strip()
        if not file_path:
            print("❌  File path cannot be empty.")
            raise SystemExit(1)
        if not os.path.isfile(file_path):
            print(f"❌  File not found: {file_path}")
            raise SystemExit(1)
        si = input("Start index (default: 0): ").strip()
        start_index = int(si) if si else 0

    # Max unfollows
    mu = input("Max unfollows this session (default: 50): ").strip()
    max_unfollows = int(mu) if mu else 50

    # Browser
    print()
    print("Browser to use?")
    print("  1) Chrome (default)")
    print("  2) Firefox")
    browser_choice = input("Choose [1/2] (default: 1): ").strip() or "1"
    browser = "firefox" if browser_choice == "2" else "chrome"

    print()
    print("=" * 60)
    print(f"  Username:      {username}")
    print(f"  Mode:          {'scrape' if scrape else f'file ({file_path})'}")
    print(f"  Max unfollows: {max_unfollows}")
    print(f"  Browser:       {browser}")
    print(f"  Login:         manual (you log in yourself)")
    print("=" * 60)
    print()

    confirm = input("Start? [Y/n]: ").strip().lower()
    if confirm and confirm != "y":
        print("Cancelled.")
        raise SystemExit(0)

    return BotConfig(
        username=username,
        manual_login=True,
        browser=browser,
        max_unfollows=max_unfollows,
        start_index=start_index,
        file=file_path,
        scrape=scrape,
    )


# ─── CLI ────────────────────────────────────────────────────────────────────────

def _build_config_from_args() -> BotConfig:
    import argparse
    import sys

    # If no arguments at all, run interactive setup
    if len(sys.argv) == 1:
        return _interactive_setup()

    p = argparse.ArgumentParser(
        description="Instagram Unfollow Bot — remove accounts that don't follow you back.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Auth
    p.add_argument("-u", "--username", default=os.getenv("INSTAGRAM_USERNAME"),
                   help="Instagram username (or set INSTAGRAM_USERNAME)")
    p.add_argument("-m", "--manual-login", action="store_true", default=True,
                   help="Log in manually in the browser (default: True)")
    p.add_argument("--auto-login", action="store_true",
                   help="Use automated login (requires INSTAGRAM_PASSWORD env var)")

    # Mode
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-f", "--file", help="File of usernames to unfollow (one per line)")
    mode.add_argument("--scrape", action="store_true",
                      help="Scrape followers/following live and unfollow non-followers")

    # Tuning
    p.add_argument("-n", "--max-unfollows", type=int, default=50,
                   help="Max unfollows per session (default: 50)")
    p.add_argument("-s", "--start-index", type=int, default=0,
                   help="Resume index into the file (default: 0)")
    p.add_argument("--min-delay", type=float, default=30,
                   help="Min seconds between unfollows (default: 30)")
    p.add_argument("--max-delay", type=float, default=60,
                   help="Max seconds between unfollows (default: 60)")
    p.add_argument("-b", "--browser", choices=["chrome", "firefox"], default="chrome",
                   help="Browser to use (default: chrome)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print extra debug output")

    args = p.parse_args()

    # If username still missing, prompt for it
    username = args.username
    if not username:
        username = input("Instagram username: ").strip()
        if not username:
            p.error("Username is required. Use -u USERNAME or set INSTAGRAM_USERNAME.")

    manual = not args.auto_login
    password = os.getenv("INSTAGRAM_PASSWORD") if not manual else None

    return BotConfig(
        username=username,
        password=password,
        manual_login=manual,
        browser=args.browser,
        max_unfollows=args.max_unfollows,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        start_index=args.start_index,
        file=args.file,
        scrape=args.scrape,
        verbose=args.verbose,
    )


def main():
    cfg = _build_config_from_args()

    logger.info("Starting Instagram Unfollow Bot")
    logger.info("  User:    %s", cfg.username)
    logger.info("  Browser: %s", cfg.browser)
    logger.info("  Login:   %s", "manual" if cfg.manual_login else "auto")
    logger.info("  Limit:   %d unfollows", cfg.max_unfollows)
    if cfg.file:
        logger.info("  File:    %s (start=%d)", cfg.file, cfg.start_index)
    logger.info("  Delay:   %.0f–%.0fs", cfg.min_delay, cfg.max_delay)

    with InstaBot(cfg) as bot:
        bot.run()


if __name__ == "__main__":
    main()
