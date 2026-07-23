from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from base import PlatformScraper, dedupe_preserve_order


class PerplexityScraper(PlatformScraper):
    platform_name = "perplexity"
    start_url = "https://www.perplexity.ai/"

    # Minimum seconds between prompts for this platform (overrides --min-delay if higher)
    RATE_LIMIT_DELAY: float = 12.0

    INTERNAL_HOSTS = {
        "perplexity.ai",
        "www.perplexity.ai",
        "pplx.ai",
        "www.pplx.ai",
    }

    PROMPT_SELECTORS = [
        "textarea[placeholder*='Ask']",
        "textarea[aria-label*='Ask']",
        "textarea",
        "[contenteditable='true'][role='textbox']",
        "[contenteditable='true']",
    ]

    STOP_SELECTORS = [
        "button[aria-label*='Stop' i]",
        "button:has-text('Stop')",
        "[aria-label*='Stop generating' i]",
        "text=/stop generating/i",
    ]

    def login_if_needed(self) -> None:
        page = self.require_page()
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self._find_prompt_box(timeout=900_000)

    def submit_prompt(self, prompt: str) -> None:
        page = self.require_page()
        # Always start from homepage so each prompt is a fresh conversation
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self.handle_rate_limit()
        box = self._find_prompt_box(timeout=45_000)
        before_url_count = self._external_url_count()

        # Use JS to focus and click — bypasses any overlay interception
        try:
            box.evaluate("el => { el.focus(); el.click(); }")
        except PlaywrightError:
            pass

        # Try fill first, fall back to keyboard typing
        try:
            box.fill(prompt, timeout=10_000)
        except PlaywrightError:
            try:
                # Select all and type via keyboard
                page.keyboard.press("Control+a")
                page.keyboard.type(prompt, delay=10)
            except PlaywrightError:
                pass

        submitted = self._click_submit_button()
        if not submitted:
            page.keyboard.press("Enter")

        self._wait_for_generation_to_start_or_response_change(before_url_count)
        self._wait_for_generation_to_finish()

    def get_citation_urls(self) -> list[str]:
        page = self.require_page()
        hrefs = page.locator("main a[href], article a[href], a[href][target='_blank']").evaluate_all(
            "(links) => links.map((link) => link.href)"
        )
        urls = [url for href in hrefs if (url := self._clean_external_url(href))]
        return dedupe_preserve_order(urls)

    def get_response_text(self) -> str:
        """Return the full AI-generated answer text from the current Perplexity response."""
        page = self.require_page()
        # Try to grab just the answer prose blocks (Perplexity renders answers in .prose or [data-testid] blocks)
        for selector in [
            "[data-testid='answer-content']",
            ".prose",
            "main .answer",
            "main",
        ]:
            try:
                locator = page.locator(selector).first
                if locator.count():
                    text = locator.inner_text(timeout=2_000).strip()
                    if text:
                        return text
            except PlaywrightError:
                continue
        return self._main_text()

    def _find_prompt_box(self, timeout: int):
        page = self.require_page()
        deadline = time.monotonic() + (timeout / 1000)
        challenge_notice_printed = False

        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("Perplexity page closed while waiting for the prompt textbox.")

            # Check prompt selectors first to avoid blocking on false-positive security challenge text
            for selector in self.PROMPT_SELECTORS:
                locator = page.locator(selector).last
                try:
                    if locator.count() and locator.is_visible(timeout=1_000):
                        return locator
                except PlaywrightError:
                    continue

            if self._security_challenge_visible():
                if not challenge_notice_printed:
                    print(
                        "Perplexity is showing a human verification challenge. "
                        "Complete it manually in the browser window; the scraper will continue afterward."
                    )
                    challenge_notice_printed = True
                deadline = time.monotonic() + (timeout / 1000)
                time.sleep(2.0)
                continue

            try:
                page.wait_for_load_state("networkidle", timeout=2_000)
            except PlaywrightTimeoutError as exc:
                pass
            time.sleep(0.5)

        raise RuntimeError(
            "Could not find Perplexity prompt textbox. If a Cloudflare/human verification "
            "task is visible, rerun headful and complete it manually."
        )

    def _security_challenge_visible(self) -> bool:
        page = self.require_page()
        try:
            text = page.locator("body").inner_text(timeout=1_000)
        except PlaywrightError:
            return False
        return bool(
            re.search(
                r"(performing security verification|verify you are human|just a moment|cloudflare)",
                text,
                re.I,
            )
        )

    def _click_submit_button(self) -> bool:
        page = self.require_page()
        selectors = [
            "button[aria-label*='Submit' i]",
            "button[aria-label*='Send' i]",
            "button:has-text('Submit')",
            "button:has-text('Ask')",
        ]
        for selector in selectors:
            button = page.locator(selector).last
            try:
                if button.count() and button.is_visible(timeout=1_000) and button.is_enabled(timeout=1_000):
                    button.click(timeout=5_000)
                    return True
            except PlaywrightError:
                continue
        return False

    def _wait_for_generation_to_start_or_response_change(self, before_url_count: int) -> None:
        page = self.require_page()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by Perplexity — waited 3 minutes before retry.")
            if self._any_stop_button_visible():
                return
            if self._external_url_count() > before_url_count:
                return
            try:
                if page.locator("main").inner_text(timeout=1_000).strip():
                    return
            except PlaywrightError:
                pass
            time.sleep(0.35)

    def _wait_for_generation_to_finish(self) -> None:
        page = self.require_page()
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except PlaywrightTimeoutError:
            pass

        deadline = time.monotonic() + 180
        stable_rounds = 0
        previous_text = ""
        while time.monotonic() < deadline:
            if self._any_stop_button_visible():
                stable_rounds = 0
                time.sleep(0.75)
                continue

            current_text = self._main_text()
            if current_text and current_text == previous_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                previous_text = current_text

            if stable_rounds >= 3:
                return

            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by Perplexity — waited 3 minutes before retry.")

            time.sleep(1.0)

        raise TimeoutError("Timed out waiting for Perplexity response to finish.")

    def _dismiss_modal(self) -> None:
        """Close any blocking overlay or modal dialog using JS DOM removal."""
        page = self.require_page()

        # 1. Try Escape key first (works for most dialogs)
        try:
            page.keyboard.press("Escape")
            time.sleep(0.4)
        except PlaywrightError:
            pass

        # 2. Try clicking common close/dismiss buttons
        close_selectors = [
            "button[aria-label='Close']",
            "button[aria-label='Dismiss']",
            "button:has-text('Maybe later')",
            "button:has-text('No thanks')",
            "button:has-text('Skip')",
            "button:has-text('Got it')",
            "button:has-text('Dismiss')",
            "[data-testid='modal-close']",
            "[role='dialog'] button",
        ]
        for selector in close_selectors:
            try:
                btn = page.locator(selector).first
                if btn.count() and btn.is_visible(timeout=300):
                    btn.evaluate("el => el.click()")
                    time.sleep(0.4)
                    break
            except PlaywrightError:
                continue

        # 3. Forcibly remove any full-screen fixed overlay elements from the DOM via JS
        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll('div.fixed.inset-0').forEach(el => {
                        // Only remove backdrop overlays (bg-black or semi-transparent), not the whole UI
                        const style = window.getComputedStyle(el);
                        if (
                            el.getAttribute('aria-hidden') === 'true' ||
                            el.getAttribute('data-aria-hidden') === 'true' ||
                            (style.backgroundColor && style.backgroundColor !== 'rgba(0, 0, 0, 0)')
                        ) {
                            el.remove();
                        }
                    });
                }
            """)
        except PlaywrightError:
            pass

    def _any_stop_button_visible(self) -> bool:
        page = self.require_page()
        for selector in self.STOP_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=500):
                    return True
            except PlaywrightError:
                continue
        return False

    def _main_text(self) -> str:
        page = self.require_page()
        try:
            return page.locator("main").inner_text(timeout=2_000).strip()
        except PlaywrightError:
            return page.locator("body").inner_text(timeout=2_000).strip()

    def _external_url_count(self) -> int:
        return len(self.get_citation_urls())

    def _clean_external_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            return ""

        host = parsed.netloc.lower()
        if host in self.INTERNAL_HOSTS or host.endswith(".perplexity.ai"):
            query = parse_qs(parsed.query)
            for key in ("url", "u", "target"):
                if key in query and query[key]:
                    return self._clean_external_url(unquote(query[key][0]))
            return ""

        if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?)($|\?)", parsed.path, re.I):
            return ""

        return parsed._replace(fragment="").geturl()
