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
        self._enable_web_search()
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
        page = self.require_page()
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if self._any_stop_button_visible():
                return
            try:
                send_btn = page.locator("button[data-testid='send-button']").first
                if send_btn.count() and not send_btn.is_enabled(timeout=300):
                    return
            except PlaywrightError:
                pass
            current_text = self._main_text()
            if current_text and current_text != before_text and len(current_text) > len(before_text):
                return
            time.sleep(0.4)
        raise TimeoutError("Timed out waiting for ChatGPT response to start.")

    def _wait_for_generation_to_finish(self) -> None:
        page = self.require_page()

        # Wait up to 45s for generation to START (stop button appears)
        try:
            page.wait_for_selector(
                "button[data-testid='stop-button']",
                state="visible", timeout=45_000
            )
        except (PlaywrightError, PlaywrightTimeoutError):
            pass  # too fast or different selector — continue anyway

        # Wait for generation to END
        deadline = time.monotonic() + 240
        stable_rounds = 0
        previous_text = ""

        while time.monotonic() < deadline:
            if self._any_stop_button_visible():
                stable_rounds = 0
                time.sleep(0.75)
                continue

            # Primary signal: good-response button appears only when response is complete
            try:
                done_btn = page.locator("[data-testid='good-response-turn-action-button']").last
                if done_btn.count() and done_btn.is_visible(timeout=300):
                    time.sleep(0.5)
                    return
            except PlaywrightError:
                pass

            # Fallback: last assistant message text stability
            try:
                last_msg = page.locator("[data-message-author-role='assistant']").last
                current_text = last_msg.inner_text(timeout=2_000).strip() if last_msg.count() else self._main_text()
            except PlaywrightError:
                current_text = self._main_text()

            if current_text and current_text == previous_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                previous_text = current_text

            if stable_rounds >= 3:
                return
            time.sleep(0.8)

        raise TimeoutError("Timed out waiting for ChatGPT response to finish.")

    def _is_web_search_active(self) -> bool:
        """Check if web search is already active in the compose box."""
        page = self.require_page()
        try:
            return page.evaluate("""
                () => {
                    const form = document.querySelector('form') || document.querySelector('[class*="composer"]');
                    if (!form) return false;
                    const elements = form.querySelectorAll('button, [role="button"], [class*="pill"], [class*="tag"], [class*="badge"]');
                    for (const el of elements) {
                        const label = (el.getAttribute('aria-label') || '').toLowerCase();
                        const text = (el.innerText || '').toLowerCase();
                        if (label.includes('add files') || label.includes('send') || label.includes('attach')) {
                            continue;
                        }
                        if (label.includes('search') || text.includes('search') || label.includes('web') || text.includes('web') || label.includes('look up') || text.includes('look up')) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
        except PlaywrightError:
            return False

    def _enable_web_search(self) -> None:
        """Enable web search by clicking + menu -> 'Look something up' with active verification and retry."""
        if self._is_web_search_active():
            print("[chatgpt] Web search is already active.")
            return

        page = self.require_page()
        for attempt in range(2):
            try:
                # Open the + (composer-plus) menu
                plus = page.locator("button[data-testid='composer-plus-btn']").first
                if not (plus.count() and plus.is_visible(timeout=2_000)):
                    continue
                plus.evaluate("el => el.click()")
                time.sleep(1.0)

                # Find 'Look something up' inside the menu
                search_opt = page.locator(
                    "button:has-text('Look something up'), "
                    "[role='menuitem']:has-text('Look something up'), "
                    "[role='option']:has-text('Look something up')"
                ).first
                if search_opt.count() and search_opt.is_visible(timeout=1_500):
                    search_opt.evaluate("el => el.click()")
                    time.sleep(1.5)
                    
                    if self._is_web_search_active():
                        print("[chatgpt] Web search successfully enabled.")
                        return
                else:
                    # Menu didn't have the option — close it
                    page.keyboard.press("Escape")
                    time.sleep(0.5)
            except PlaywrightError:
                pass
            time.sleep(1.0)
            
        print("[chatgpt] WARNING: Failed to enable web search after retries. Response might not contain web citations.")

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
            "button:has-text('Stay logged out')",
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
