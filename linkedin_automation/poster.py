"""
LinkedIn Post Creator
Posts text content to your LinkedIn feed.
Uses the same shadow DOM awareness as the connector.
"""

import sys
import logging
import argparse

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from dotenv import load_dotenv

from . import profile_manager as pm
from . import platform_compat
from . import human_behavior as hb
from .failure_capture import capture_failure

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LinkedInPoster:
    """Post content to LinkedIn feed."""

    def __init__(self, profile_name: str = None, debug: bool = False):
        self.profile_name = profile_name
        self.debug = debug
        self.driver = None
        self.wait = None

        # Apply tunable human-behavior timing (typing/reading/scroll/break ranges)
        # from the profile config's "behavior" section so pacing is configurable
        # and consistent with the scraper/connector.
        hb.configure_behavior(pm.get_profile_config(profile_name).get("behavior"))

    def setup(self):
        """Setup browser and login."""
        logger.info("Setting up browser...")
        self.driver, profile = pm.create_driver(self.profile_name)
        self.wait = WebDriverWait(self.driver, 20)

        if not pm.login(self.driver, profile):
            raise RuntimeError("Failed to log in")

        logger.info("Logged in successfully")

    def navigate_to_feed(self):
        """Go to LinkedIn feed."""
        logger.info("Navigating to feed...")
        self.driver.get("https://www.linkedin.com/feed/")
        hb.human_sleep(3, 5)

    def create_post(self, text: str) -> bool:
        """Create a new LinkedIn post with the given text."""
        logger.info(f"Creating post: \"{text[:80]}{'...' if len(text) > 80 else ''}\"")

        # Step 1: Click "Start a post" to open the post modal
        if not self._open_post_modal():
            capture_failure(self.driver, "post_modal_failed", self.profile_name)
            return False

        hb.human_sleep(1.5, 2.5)

        # Step 2: Type the post content
        if not self._type_post_content(text):
            capture_failure(self.driver, "post_typing_failed", self.profile_name)
            return False

        hb.human_sleep(1.0, 2.0)

        # Step 3: Click Post
        if not self._click_post_button():
            capture_failure(self.driver, "post_submit_failed", self.profile_name)
            return False

        logger.info("✓ Post published successfully!")
        return True

    def _open_post_modal(self) -> bool:
        """Click 'Start a post' to open the post creation modal."""

        # Strategy 1: Button with "Start a post" text
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                try:
                    text = btn.text.strip().lower()
                    if "start a post" in text:
                        if btn.is_displayed():
                            logger.info("  Found 'Start a post' button")
                            hb.human_click(self.driver, btn)
                            return True
                except Exception:
                    continue
        except Exception:
            logger.debug("Strategy 1 (button text) failed", exc_info=True)

        # Strategy 2: The post prompt area (div/span with "Start a post")
        try:
            el = self.driver.find_element(
                By.XPATH, "//*[contains(text(), 'Start a post') or contains(text(), 'start a post')]"
            )
            if el.is_displayed():
                logger.info("  Found post prompt element")
                hb.human_click(self.driver, el)
                return True
        except Exception:
            logger.debug("Strategy 2 (post prompt element) failed", exc_info=True)

        # Strategy 3: aria-label or placeholder
        for sel in [
            "button[aria-label*='Start a post']",
            "div[aria-label*='Start a post']",
            "button[aria-label*='Create a post']",
            "div[role='button'][aria-placeholder*='Start']",
        ]:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    logger.info(f"  Found post trigger: {sel}")
                    hb.human_click(self.driver, el)
                    return True
            except Exception:
                continue

        # Strategy 4: Shadow DOM
        try:
            clicked = self.driver.execute_script("""
                // Check regular DOM first
                var allEls = document.querySelectorAll('button, div[role="button"], span');
                for (var i = 0; i < allEls.length; i++) {
                    var text = allEls[i].textContent.trim().toLowerCase();
                    if (text.includes('start a post') && allEls[i].offsetParent !== null) {
                        allEls[i].click();
                        return 'regular DOM';
                    }
                }
                // Check shadow DOMs
                var all = document.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    if (all[i].shadowRoot) {
                        var els = all[i].shadowRoot.querySelectorAll('button, div[role="button"], span');
                        for (var j = 0; j < els.length; j++) {
                            var text = els[j].textContent.trim().toLowerCase();
                            if (text.includes('start a post') && els[j].offsetParent !== null) {
                                els[j].click();
                                return 'shadow DOM';
                            }
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  Opened post modal via {clicked}")
                return True
        except Exception:
            logger.debug("Strategy 4 (shadow DOM) failed", exc_info=True)

        logger.error("Could not find 'Start a post' button")
        return False

    def _type_post_content(self, text: str) -> bool:
        """Type the post content into the editor."""

        # Wait for the modal/editor to appear
        hb.human_sleep(1.0, 1.5)

        # Strategy 1: contenteditable div (standard LinkedIn post editor)
        try:
            editor = self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']")
            ))
            if editor.is_displayed():
                logger.info("  Found post editor (contenteditable)")
                hb.human_click(self.driver, editor)
                hb.human_sleep(0.3, 0.5)
                hb.type_like_human(self.driver, editor, text)
                return True
        except TimeoutException:
            logger.debug("Strategy 1 (editor lookup) timed out; trying Strategy 2", exc_info=True)

        # Strategy 2: Any contenteditable in a modal/dialog
        try:
            editables = self.driver.find_elements(
                By.CSS_SELECTOR, "div[contenteditable='true']"
            )
            for ed in editables:
                if ed.is_displayed():
                    logger.info("  Found contenteditable div")
                    hb.human_click(self.driver, ed)
                    hb.human_sleep(0.3, 0.5)
                    hb.type_like_human(self.driver, ed, text)
                    return True
        except Exception:
            logger.debug("Strategy 2 (contenteditable div) failed", exc_info=True)

        # Strategy 3: textarea fallback
        try:
            textareas = self.driver.find_elements(By.CSS_SELECTOR, "textarea")
            for ta in textareas:
                if ta.is_displayed():
                    logger.info("  Found textarea")
                    hb.human_click(self.driver, ta)
                    hb.human_sleep(0.3, 0.5)
                    hb.type_like_human(self.driver, ta, text)
                    return True
        except Exception:
            logger.debug("Strategy 3 (textarea) failed", exc_info=True)

        # Strategy 4: Shadow DOM editor
        try:
            typed = self.driver.execute_script("""
                var hosts = document.querySelectorAll('*');
                for (var i = 0; i < hosts.length; i++) {
                    if (hosts[i].shadowRoot) {
                        var editors = hosts[i].shadowRoot.querySelectorAll(
                            'div[contenteditable="true"], textarea'
                        );
                        for (var j = 0; j < editors.length; j++) {
                            if (editors[j].offsetParent !== null) {
                                editors[j].focus();
                                editors[j].click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            """)
            if typed:
                # Type with human-like timing into the JS-focused shadow-DOM editor
                # (no WebElement to target, so use the focused-element variant).
                hb.type_like_human_keys(self.driver, text)
                logger.info("  Typed into shadow DOM editor")
                return True
        except Exception:
            logger.debug("Strategy 4 (shadow DOM editor) failed", exc_info=True)

        # Strategy 5: aria-label based
        for label in ["Text editor", "Write", "Post", "Share"]:
            try:
                el = self.driver.find_element(
                    By.CSS_SELECTOR, f"div[aria-label*='{label}'][contenteditable='true']"
                )
                if el.is_displayed():
                    hb.human_click(self.driver, el)
                    hb.human_sleep(0.3, 0.5)
                    hb.type_like_human(self.driver, el, text)
                    logger.info(f"  Typed into editor (aria-label: {label})")
                    return True
            except Exception:
                continue

        logger.error("Could not find post text editor")
        return False

    def _click_post_button(self) -> bool:
        """Click the Post button to publish."""

        # Strategy 1: Regular DOM button with "Post" text
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buttons:
                try:
                    text = btn.text.strip()
                    if text.lower() == "post":
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info("  Found 'Post' button")
                            hb.human_click(self.driver, btn)
                            hb.human_sleep(2.0, 3.0)
                            return True
                except Exception:
                    continue
        except Exception:
            logger.debug("Strategy 1 (Post button text) failed", exc_info=True)

        # Strategy 2: Shadow DOM Post button
        try:
            clicked = self.driver.execute_script("""
                // Regular DOM
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim();
                    if (text === 'Post' && buttons[i].offsetParent !== null && !buttons[i].disabled) {
                        buttons[i].click();
                        return 'regular';
                    }
                }
                // Shadow DOM
                var hosts = document.querySelectorAll('*');
                for (var i = 0; i < hosts.length; i++) {
                    if (hosts[i].shadowRoot) {
                        var btns = hosts[i].shadowRoot.querySelectorAll('button');
                        for (var b = 0; b < btns.length; b++) {
                            var text = btns[b].textContent.trim();
                            if (text === 'Post' && btns[b].offsetParent !== null && !btns[b].disabled) {
                                btns[b].click();
                                return 'shadow';
                            }
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.info(f"  Clicked Post button ({clicked} DOM)")
                hb.human_sleep(2.0, 3.0)
                return True
        except Exception:
            logger.debug("Strategy 2 (shadow DOM Post button) failed", exc_info=True)

        # Strategy 3: aria-label
        for sel in ["button[aria-label='Post']", "button[aria-label='Post for anyone']"]:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed() and btn.is_enabled():
                    hb.human_click(self.driver, btn)
                    hb.human_sleep(2.0, 3.0)
                    return True
            except Exception:
                continue

        logger.error("Could not find Post button")
        return False

    def run(self, text: str) -> bool:
        """Full flow: setup, navigate, post."""
        try:
            self.setup()
            self.navigate_to_feed()
            result = self.create_post(text)
            hb.human_sleep(2.0, 3.0)
            return result
        except Exception as e:
            logger.error(f"Error: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
        finally:
            if self.driver:
                self.driver.quit()
                logger.info("Browser closed")


def main():
    """CLI entry point: publish a post to the LinkedIn feed."""
    # A stop from the dashboard arrives as SIGTERM, and Python skips every
    # finally block when a signal kills the process. Without this the
    # driver.quit() below never runs and Chrome is orphaned holding the
    # profile lock, which blocks every later run and every login.
    platform_compat.exit_cleanly_on_termination()

    parser = argparse.ArgumentParser(description='LinkedIn Post Creator')
    parser.add_argument('text', nargs='?', help='Post text (or use --file)')
    parser.add_argument('--file', type=str, help='Read post text from file')
    parser.add_argument('--profile', type=str, default=None, help='LinkedIn profile name')
    parser.add_argument('--debug', action='store_true', help='Debug mode')

    args = parser.parse_args()

    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide post text as argument or use --file")

    if not text:
        parser.error("Post text cannot be empty")

    poster = LinkedInPoster(profile_name=args.profile, debug=args.debug)
    success = poster.run(text)

    if success:
        print("\n✓ Post published!")
    else:
        print("\n✗ Failed to publish post")
        sys.exit(1)


if __name__ == "__main__":
    main()
