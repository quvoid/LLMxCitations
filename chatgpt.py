from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from base import PlatformScraper, dedupe_preserve_order


class ChatGPTScraper(PlatformScraper):
    platform_name = "chatgpt"
    start_url = "https://chatgpt.com/"

    # Minimum seconds between prompts for this platform (overrides --min-delay if higher)
    RATE_LIMIT_DELAY: float = 20.0

    INTERNAL_HOSTS = {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "openai.com",
        "www.openai.com",
        "auth.openai.com",
        "platform.openai.com",
    }

    PROMPT_SELECTORS = [
        "#prompt-textarea",
        "[data-testid='prompt-textarea']",
        "[contenteditable='true'][id='prompt-textarea']",
        "[contenteditable='true'][data-testid='prompt-textarea']",
        "textarea[placeholder*='Message']",
        "[contenteditable='true'][role='textbox']",
    ]

    SEND_SELECTORS = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send prompt' i]",
        "button[aria-label*='Send message' i]",
        "button[aria-label='Send']",
    ]

    STOP_SELECTORS = [
        "button[data-testid='stop-button']",
        "main button[aria-label*='Stop generating' i]",
        "main button[aria-label='Stop']",
    ]

    def login_if_needed(self) -> None:
        page = self.require_page()
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self._find_prompt_box(timeout=900_000)

    def submit_prompt(self, prompt: str) -> None:
        page = self.require_page()
        page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        self._dismiss_modal()
        self._handle_rate_limit()  # check for rate limit right on page load
        box = self._find_prompt_box(timeout=900_000)
        before_text = self._main_text()

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
                page.keyboard.press("Control+a")
                page.keyboard.type(prompt, delay=10)
            except PlaywrightError:
                pass

        if not self._click_send_button():
            page.keyboard.press("Enter")

        self._wait_for_response_to_start(before_text)
        self._wait_for_generation_to_finish()

    def get_citation_urls(self) -> list[str]:
        page = self.require_page()

        # ChatGPT hides source URLs inside a collapsible 'Sources' panel — expand it first
        try:
            sources_btn = page.locator(
                "[class*='footnote'], button[aria-label*='source' i], "
                "button:has-text('Sources'), [aria-label='Sources']"
            ).last
            if sources_btn.count() and sources_btn.is_visible(timeout=1_500):
                sources_btn.evaluate("el => el.click()")
                time.sleep(2.0)  # wait for panel to load URLs
        except PlaywrightError:
            pass

        # Now extract all external links + data-url attributes
        try:
            all_hrefs: list[str] = page.evaluate("""
                () => {
                    const hrefs = new Set();
                    document.querySelectorAll('a[href]').forEach(el => hrefs.add(el.href));
                    document.querySelectorAll('[data-url],[data-href],[data-source-url]').forEach(el => {
                        const u = el.getAttribute('data-url')
                                || el.getAttribute('data-href')
                                || el.getAttribute('data-source-url');
                        if (u) hrefs.add(u);
                    });
                    return Array.from(hrefs);
                }
            """)
        except PlaywrightError:
            all_hrefs = []

        # Also extract any URLs written inline in the assistant response text
        try:
            text = self.get_response_text()
            if text:
                for raw_url in re.findall(r'https?://(?:[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+)', text):
                    all_hrefs.append(raw_url.rstrip('.,;:)\'"'))
        except Exception:
            pass

        urls = [url for href in all_hrefs if (url := self._clean_external_url(href))]
        return dedupe_preserve_order(urls)

    def get_response_text(self) -> str:
        """Return the full AI-generated answer text from the last ChatGPT response."""
        page = self.require_page()
        for selector in [
            "[data-message-author-role='assistant']:last-of-type",
            "[data-message-author-role='assistant']",
            "main .markdown",
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
        challenge_notice_printed = False
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("ChatGPT page closed while waiting for the prompt textbox.")

            # Check prompt selectors first to avoid blocking on false-positive security challenge/login text
            for selector in self.PROMPT_SELECTORS:
                locator = page.locator(selector).last
                try:
                    if locator.count() and locator.is_visible(timeout=1_000):
                        return locator
                except PlaywrightError:
                    continue

            if self._login_or_challenge_visible():
                if not challenge_notice_printed:
                    print(
                        "ChatGPT may need login or human verification. "
                        "Complete it manually in the browser; the scraper will continue afterward."
                    )
                    challenge_notice_printed = True
                deadline = time.monotonic() + (timeout / 1000)
                time.sleep(2.0)

            try:
                page.wait_for_load_state("networkidle", timeout=2_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(0.5)

        raise RuntimeError("Could not find ChatGPT prompt textbox.")

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
        """Wait until ChatGPT navigation confirms the prompt was accepted."""
        page = self.require_page()
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by ChatGPT — waited 3 minutes before retry.")
            # Most reliable signal: URL changes from '/' to '/c/{id}' on submission
            if "/c/" in page.url:
                return
            # Fallback: stop button appeared or text grew
            if self._any_stop_button_visible():
                return
            current_text = self._main_text()
            if current_text and current_text != before_text and len(current_text) > len(before_text):
                return
            time.sleep(0.3)
        raise TimeoutError("Timed out waiting for ChatGPT response to start.")

    def _wait_for_generation_to_finish(self) -> None:
        page = self.require_page()

        # Short window (5s) for the stop button to appear as confirmation
        try:
            page.wait_for_selector(
                "button[data-testid='stop-button']",
                state="visible", timeout=5_000
            )
        except (PlaywrightError, PlaywrightTimeoutError):
            pass

        deadline = time.monotonic() + 180
        stable_rounds = 0
        previous_text = ""

        while time.monotonic() < deadline:
            # Still generating if stop button is visible
            try:
                stop = page.locator("button[data-testid='stop-button']").first
                if stop.count() and stop.is_visible(timeout=300):
                    stable_rounds = 0
                    time.sleep(0.5)
                    continue
            except PlaywrightError:
                pass

            # Primary completion signals — appear only after generation finishes
            try:
                done = page.locator(
                    "[data-testid='good-response-turn-action-button'], "
                    "[data-testid='bad-response-turn-action-button'], "
                    "[data-testid='copy-turn-action-button']"
                ).last
                if done.count() and done.is_visible(timeout=300):
                    time.sleep(0.5)
                    return
            except PlaywrightError:
                pass

            # Fallback: last assistant message text stability (3 × 0.8s = 2.4s stable)
            try:
                last_msg = page.locator("[data-message-author-role='assistant']").last
                current_text = last_msg.inner_text(timeout=2_000).strip() if last_msg.count() else ""
            except PlaywrightError:
                current_text = ""

            if current_text and current_text == previous_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                previous_text = current_text

            if stable_rounds >= 3:
                return

            # Check for rate limit modal mid-generation
            if self.handle_rate_limit():
                raise TimeoutError("Rate limited by ChatGPT — waited 3 minutes before retry.")

            time.sleep(0.8)

        raise TimeoutError("Timed out waiting for ChatGPT response to finish.")

    _handle_rate_limit = PlatformScraper.handle_rate_limit


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
            "button[aria-label='close']",
            "[data-testid='modal-close']",
            "button:has-text('Maybe later')",
            "button:has-text('No thanks')",
            "button:has-text('Not now')",
            "button:has-text('Skip')",
            "button:has-text('Got it')",
            "button:has-text('Dismiss')",
            "button:has-text('Stay logged out')",
            "button:has-text('Later')",
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

        # 3. JS: search dialogs for close buttons, explicitly skip voice/start/try buttons
        try:
            page.evaluate("""
                () => {
                    const SKIP = ['voice', 'start', 'try', 'enable', 'microphone', 'record'];
                    const CLOSE = ['close', 'dismiss', 'skip', 'later', 'no thanks', 'not now', 'got it'];
                    const dialogs = document.querySelectorAll('[role="dialog"], [data-state="open"]');
                    for (const dlg of dialogs) {
                        for (const btn of dlg.querySelectorAll('button')) {
                            const lbl = (btn.getAttribute('aria-label') || btn.innerText || '').toLowerCase().trim();
                            if (SKIP.some(w => lbl.includes(w))) continue;
                            if (CLOSE.some(w => lbl.includes(w)) || lbl === 'x') {
                                btn.click(); return;
                            }
                        }
                    }
                }
            """)
        except PlaywrightError:
            pass

        # 4. Force-remove blocking full-screen overlay divs
        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll('div.fixed.inset-0, div[data-state="open"][aria-hidden="true"]').forEach(el => {
                        const s = window.getComputedStyle(el);
                        if (el.getAttribute('aria-hidden') === 'true' ||
                            (s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)')) {
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
            try:
                return page.locator("body").inner_text(timeout=2_000).strip()
            except PlaywrightError:
                return ""

    def _login_or_challenge_visible(self) -> bool:
        page = self.require_page()
        try:
            text = page.locator("body").inner_text(timeout=1_000)
        except PlaywrightError:
            return False
        return bool(re.search(r"(log in|sign up|verify you are human|just a moment|cloudflare)", text, re.I))

    def _clean_external_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            return ""

        host = parsed.netloc.lower()
        if host in self.INTERNAL_HOSTS or host.endswith(".chatgpt.com") or host.endswith(".openai.com"):
            query = parse_qs(parsed.query)
            for key in ("url", "u", "q", "target"):
                if key in query and query[key]:
                    return self._clean_external_url(unquote(query[key][0]))
            return ""

        if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?)($|\?)", parsed.path, re.I):
            return ""

        return parsed._replace(fragment="").geturl()
