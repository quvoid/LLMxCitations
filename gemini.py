from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from base import PlatformScraper, dedupe_preserve_order


class GeminiScraper(PlatformScraper):
    platform_name = "gemini"
    start_url = "https://gemini.google.com/app"

    INTERNAL_HOSTS = {
        "gemini.google.com",
        "gemini.google",
        "business.gemini.google",
        "accounts.google.com",
        "myaccount.google.com",
        "support.google.com",
        "google.com",
        "www.google.com",
    }

    PROMPT_SELECTORS = [
        "rich-textarea [contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
        "div[aria-label*='Enter a prompt' i][contenteditable='true']",
        "textarea[aria-label*='Enter a prompt' i]",
        "[contenteditable='true']",
    ]

    SEND_SELECTORS = [
        "button[aria-label*='Send message' i]",
        "button[aria-label*='Send' i]",
        "button:has(mat-icon:has-text('send'))",
    ]

    STOP_SELECTORS = [
        "button[aria-label*='Stop' i]",
        "button:has-text('Stop')",
        "button:has(mat-icon:has-text('stop'))",
    ]

    # Appended to every prompt so Gemini includes source URLs inline
    PROMPT_SUFFIX = (
        "\n\nImportant: For every fact or recommendation, please include the "
        "full source URL (e.g. https://example.com/page) so I can verify the information."
    )

    # Regex to extract URLs written inline in the response text
    _URL_RE = re.compile(
        r'https?://(?:[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%])+',
        re.I
    )

    def login_if_needed(self) -> None:
        page = self.require_page()
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self._find_prompt_box(timeout=900_000)

    def submit_prompt(self, prompt: str) -> None:
        page = self.require_page()
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self.handle_rate_limit()
        box = self._find_prompt_box(timeout=900_000)
        before_text = self._main_text()

        # Use JS to focus and click — bypasses any overlay interception
        try:
            box.evaluate("el => { el.focus(); el.click(); }")
        except PlaywrightError:
            pass

        # Append suffix to ask Gemini to include source URLs inline
        augmented = prompt + self.PROMPT_SUFFIX

        # Try fill first, fall back to keyboard typing
        try:
            box.fill(augmented, timeout=10_000)
        except PlaywrightError:
            try:
                page.keyboard.press("Control+a")
                page.keyboard.type(augmented, delay=10)
            except PlaywrightError:
                pass

        if not self._click_send_button():
            page.keyboard.press("Enter")

        self._wait_for_response_to_start(before_text)
        self._wait_for_generation_to_finish()

    def get_citation_urls(self) -> list[str]:
        """Extract citation URLs from Gemini response.
        
        Gemini free tier does not expose sources in DOM (sources-list is always empty).
        Primary strategy: extract URLs written inline in the response text.
        Secondary strategy: scrape any <a href> links from the page DOM.
        """
        page = self.require_page()
        found: list[str] = []

        # 1. Extract URLs from response text (primary — works on free tier)
        try:
            for selector in [
                "model-response:last-of-type",
                "model-response",
                "bard-sidenav-content",
                "main",
            ]:
                locator = page.locator(selector).last
                if locator.count():
                    text = locator.inner_text(timeout=3_000).strip()
                    if text:
                        for raw_url in self._URL_RE.findall(text):
                            # Strip trailing punctuation
                            raw_url = raw_url.rstrip('.,;:)\'"')
                            cleaned = self._clean_external_url(raw_url)
                            if cleaned:
                                found.append(cleaned)
                        break
        except PlaywrightError:
            pass

        # 2. Fallback: DOM <a href> scrape (works if Gemini ever shows sources in DOM)
        try:
            hrefs = page.locator(
                "sources-list a[href], source-chip a[href], "
                "main a[href], bard-sidenav-content a[href]"
            ).evaluate_all("(links) => links.map(l => l.href)")
            for href in hrefs:
                cleaned = self._clean_external_url(href)
                if cleaned:
                    found.append(cleaned)
        except PlaywrightError:
            pass

        return dedupe_preserve_order(found)

    def get_response_text(self) -> str:
        """Return the full AI-generated answer text from the last Gemini response."""
        page = self.require_page()
        for selector in [
            "model-response:last-of-type .response-content",
            "model-response:last-of-type",
            "model-response",
            "bard-sidenav-content",
            "main",
        ]:
            try:
                locator = page.locator(selector).last
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
        login_notice_printed = False

        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("Gemini page closed while waiting for the prompt textbox.")
            # Check prompt selectors first to avoid blocking on false-positive security challenge/login text
            for selector in self.PROMPT_SELECTORS:
                locator = page.locator(selector).last
                try:
                    if locator.count() and locator.is_visible(timeout=1_000):
                        return locator
                except PlaywrightError:
                    continue

            if self._login_or_challenge_visible():
                if not login_notice_printed:
                    print(
                        "Gemini may need login, consent, or human verification. "
                        "Complete it manually in the browser; the scraper will continue afterward."
                    )
                    login_notice_printed = True
                deadline = time.monotonic() + (timeout / 1000)
                time.sleep(2.0)

            try:
                page.wait_for_load_state("networkidle", timeout=2_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(0.5)

        raise RuntimeError("Could not find Gemini prompt textbox.")

    def _click_send_button(self) -> bool:
        page = self.require_page()
        for selector in self.SEND_SELECTORS:
            button = page.locator(selector).last
            try:
                if button.count() and button.is_visible(timeout=1_000) and button.is_enabled(timeout=1_000):
                    button.click(timeout=5_000)
                    return True
            except PlaywrightError:
                continue
        return False

    def _wait_for_response_to_start(self, before_text: str) -> None:
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by Gemini — waited 3 minutes before retry.")
            if self._any_stop_button_visible():
                return
            current_text = self._main_text()
            if current_text and current_text != before_text and len(current_text) > len(before_text):
                return
            time.sleep(0.5)
        raise TimeoutError("Timed out waiting for Gemini response to start.")

    def _wait_for_generation_to_finish(self) -> None:
        page = self.require_page()
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except PlaywrightTimeoutError:
            pass

        deadline = time.monotonic() + 240
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

            if stable_rounds >= 4:
                return

            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by Gemini — waited 3 minutes before retry.")

            time.sleep(1.0)

        raise TimeoutError("Timed out waiting for Gemini response to finish.")

    def _dismiss_modal(self) -> None:
        """Close any blocking overlay or modal dialog using JS DOM removal."""
        page = self.require_page()

        # 1. Try Escape key first
        try:
            page.keyboard.press("Escape")
            time.sleep(0.4)
        except PlaywrightError:
            pass

        # 2. Try clicking common close/dismiss buttons via JS
        close_selectors = [
            "button[aria-label='Close']",
            "button[aria-label='Dismiss']",
            "button:has-text('Maybe later')",
            "button:has-text('No thanks')",
            "button:has-text('Skip')",
            "button:has-text('Got it')",
            "button:has-text('Dismiss')",
            "button:has-text('I agree')",
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

        # 3. Forcibly remove any full-screen fixed overlay elements from the DOM
        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll('div.fixed.inset-0, div[data-state="open"][aria-hidden="true"]').forEach(el => {
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
        for selector in ("main", "bard-sidenav-content", "body"):
            try:
                return page.locator(selector).inner_text(timeout=2_000).strip()
            except PlaywrightError:
                continue
        return ""

    def _login_or_challenge_visible(self) -> bool:
        page = self.require_page()
        try:
            text = page.locator("body").inner_text(timeout=1_000)
        except PlaywrightError:
            return False
        return bool(re.search(r"(sign in|log in|choose an account|verify|not a robot|consent)", text, re.I))

    def _clean_external_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            return ""

        host = parsed.netloc.lower()
        # Block all Google-owned hosts: *.google.com, *.google.co.in, *.google (TLD), etc.
        if host in self.INTERNAL_HOSTS:
            return ""
        if host.endswith(".googleusercontent.com"):
            return ""
        if host.endswith(".google") or host == "google":
            return ""  # catches gemini.google, business.gemini.google, etc.
        if host.endswith(".google.com") or host.endswith(".google.co.in") or host.endswith(".google.co"):
            query = parse_qs(parsed.query)
            for key in ("url", "q", "u", "target"):
                if key in query and query[key]:
                    return self._clean_external_url(unquote(query[key][0]))
            return ""

        if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?)($|\?)", parsed.path, re.I):
            return ""

        return parsed._replace(fragment="").geturl()
