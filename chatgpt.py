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
    RATE_LIMIT_DELAY: float = 1.0


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
        "textarea#prompt-textarea",
        "div#prompt-textarea",
        "textarea",
        "[contenteditable='true'][id='prompt-textarea']",
        "[contenteditable='true'][data-testid='prompt-textarea']",
        "textarea[placeholder*='Message']",
        "[contenteditable='true'][role='textbox']",
        "[contenteditable='true']",
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

    def _check_and_handle_anonymous_limit(self) -> bool:
        """If anonymous message limit reached popup or 'Clear current chat' is displayed, handle it and start fresh."""
        page = self.require_page()
        try:
            # Handle 'Clear current chat?' popup immediately
            clear_btn = page.locator("button:has-text('Clear chat'), button:has-text('Clear current chat')").first
            if clear_btn.count() and clear_btn.is_visible(timeout=300):
                clear_btn.evaluate("el => el.click()")
                time.sleep(0.5)
                return True

            body_text = page.locator("body").inner_text(timeout=500)
            if re.search(r"(message limit reached|reached the anonymous message limit|anonymous message limit|clear current chat)", body_text, re.I):
                print("[chatgpt] Anonymous limit / Clear chat modal detected! Resetting session...", flush=True)
                # 1. Try clicking 'Clear chat' or 'New chat' if visible
                try:
                    action_btn = page.locator("button:has-text('Clear chat'), button:has-text('New chat'), a:has-text('New chat')").first
                    if action_btn.count() and action_btn.is_visible(timeout=300):
                        action_btn.evaluate("el => el.click()")
                        time.sleep(0.5)
                except Exception:
                    pass

                # 2. Clear browser storage (localStorage, sessionStorage, indexedDB)
                try:
                    page.evaluate("""
                        () => {
                            try { localStorage.clear(); } catch(e) {}
                            try { sessionStorage.clear(); } catch(e) {}
                            try {
                                if (window.indexedDB && indexedDB.databases) {
                                    indexedDB.databases().then(dbs => {
                                        for (let db of dbs) indexedDB.deleteDatabase(db.name);
                                    });
                                }
                            } catch(e) {}
                        }
                    """)
                except Exception:
                    pass

                # 3. Clear cookies
                try:
                    page.context.clear_cookies()
                except Exception:
                    pass

                # 4. Navigate to fresh ChatGPT page
                try:
                    page.goto(self.start_url, wait_until="domcontentloaded", timeout=30_000)
                    time.sleep(1.5)
                    self._dismiss_modal()
                except Exception:
                    pass
                return True
        except Exception:
            pass
        return False

    def submit_prompt(self, prompt: str) -> None:
        page = self.require_page()
        self._dismiss_modal()

        box = self._find_prompt_box(timeout=30_000)
        before_text = self._main_text()

        try:
            box.click(timeout=2_000)
        except Exception:
            pass

        box.fill(prompt)
        time.sleep(0.2)

        # Press Enter directly on prompt box
        box.press("Enter")
        time.sleep(0.4)

        # Click send button only if not already sending
        self._click_send_button()

        self._wait_for_response_to_start(before_text)
        self._wait_for_generation_to_finish()

    def get_citation_urls(self) -> list[str]:
        page = self.require_page()

        # ChatGPT hides source URLs inside a collapsible 'Sources' / citations panel — expand it first
        try:
            sources_selectors = [
                "button[data-testid*='source']",
                "button:has-text('Sources')",
                "button[aria-label*='source' i]",
                "button[aria-label*='Sources' i]",
                "[class*='footnote']",
                "[class*='citation']",
                "[data-testid='source-panel-button']",
            ]
            for sel in sources_selectors:
                sources_btn = page.locator(sel).last
                if sources_btn.count() and sources_btn.is_visible(timeout=800):
                    sources_btn.evaluate("el => el.click()")
                    time.sleep(1.0)
                    break
        except PlaywrightError:
            pass

        # Now extract all external links + data-url attributes + attribution links
        try:
            all_hrefs: list[str] = page.evaluate("""
                () => {
                    const hrefs = new Set();
                    document.querySelectorAll('a[href]').forEach(el => {
                        if (el.href && !el.href.startsWith('javascript:')) hrefs.add(el.href);
                    });
                    document.querySelectorAll('[data-url],[data-href],[data-source-url],[data-attribution-url],[data-item-url]').forEach(el => {
                        const u = el.getAttribute('data-url')
                                || el.getAttribute('data-href')
                                || el.getAttribute('data-source-url')
                                || el.getAttribute('data-attribution-url')
                                || el.getAttribute('data-item-url');
                        if (u) hrefs.add(u);
                    });
                    // Search inside assistant messages, citations, and product cards
                    document.querySelectorAll('[data-message-author-role=\"assistant\"], [class*=\"citation\"], [class*=\"product\"], [class*=\"source\"], section, article').forEach(container => {
                        container.querySelectorAll('a').forEach(a => { if (a.href) hrefs.add(a.href); });
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
            ".wm-app-threadContent [data-message-author-role='assistant']",
            ".wm-app-threadContent article",
            ".wm-app-threadContent div.markdown",
            ".wm-app-conversation article",
            "article[data-testid*='conversation-turn']",
            "main .markdown",
        ]:
            try:
                locator = page.locator(selector).last
                if locator.count() and locator.is_visible(timeout=500):
                    text = locator.inner_text(timeout=2_000).strip()
                    if text and not text.startswith("SettingsAppearance") and len(text) > 10:
                        return text
            except PlaywrightError:
                continue

        main_t = self._main_text()
        if main_t and not main_t.startswith("SettingsAppearance") and not main_t.startswith("ChatGPT:"):
            return main_t
        return ""

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
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible(timeout=500):
                    aria_lbl = (button.get_attribute("aria-label") or button.inner_text() or "").lower()
                    if any(w in aria_lbl for w in ["voice", "speech", "dictate", "mic", "record"]):
                        continue
                    button.evaluate("el => el.click()")
                    return True
            except Exception:
                continue
        return False

    def _send_prompt_if_not_started(self) -> None:
        """If response has not started and text is still in prompt box, re-trigger send."""
        page = self.require_page()
        if "/c/" in page.url or self._any_stop_button_visible():
            return
        try:
            if page.locator("[data-message-author-role='assistant'], div.markdown, article, .agent-turn").count() > 0:
                return
            box = self._find_prompt_box(timeout=1_000)
            box_text = box.evaluate("el => (el.value || el.innerText || '').trim()")
            if box_text:
                box.press("Enter")
                time.sleep(0.2)
                self._click_send_button()
        except Exception:
            pass

    def _wait_for_response_to_start(self, before_text: str) -> None:
        """Wait until ChatGPT navigation or response start confirms prompt was accepted."""
        page = self.require_page()
        deadline = time.monotonic() + 45
        last_resend_time = time.monotonic()

        while time.monotonic() < deadline:
            dismissed = self.handle_rate_limit() or self._check_and_handle_anonymous_limit()

            # Signal 1: URL changes to '/c/{id}'
            if "/c/" in page.url:
                return
            # Signal 2: Stop button appeared
            if self._any_stop_button_visible():
                return
            # Signal 3: Assistant message turn appeared (vital for unauthenticated guest mode)
            try:
                if page.locator("[data-message-author-role='assistant'], div.markdown, article, .agent-turn").count() > 0:
                    return
            except PlaywrightError:
                pass
            # Signal 4: Main page text grew
            current_text = self._main_text()
            if current_text and current_text != before_text and len(current_text) > len(before_text):
                return

            # If a modal was dismissed OR if 3s passed without response starting, re-send prompt if text is stuck in box
            now = time.monotonic()
            if dismissed or (now - last_resend_time >= 3.0):
                self._send_prompt_if_not_started()
                last_resend_time = now

            time.sleep(0.4)
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

        deadline = time.monotonic() + 45
        stable_rounds = 0
        previous_text = ""

        while time.monotonic() < deadline:
            # Still generating if stop button is visible
            try:
                stop = page.locator("button[data-testid='stop-button'], main button[aria-label*='Stop']").first
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
                    "[data-testid='copy-turn-action-button'], "
                    "button[aria-label*='Copy']"
                ).last
                if done.count() and done.is_visible(timeout=300):
                    time.sleep(0.5)
                    return
            except PlaywrightError:
                pass

            # Check if send button re-appeared (meaning streaming turn completed)
            try:
                send_btn = page.locator("button[data-testid='send-button'], button[aria-label*='Send']").first
                if send_btn.count() and send_btn.is_visible(timeout=200):
                    time.sleep(0.5)
                    return
            except PlaywrightError:
                pass

            # Fallback: assistant message / markdown container text stability
            try:
                last_msg = page.locator("[data-message-author-role='assistant'], div.markdown, article, .agent-turn").last
                current_text = last_msg.inner_text(timeout=1_000).strip() if last_msg.count() else ""
                if not current_text:
                    current_text = self._main_text()
            except PlaywrightError:
                current_text = ""

            if current_text and current_text == previous_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                previous_text = current_text

            if stable_rounds >= 2 and len(current_text) > 30:
                return



            if self.handle_rate_limit():
                stable_rounds = 0
                time.sleep(0.5)

            time.sleep(0.8)

        raise TimeoutError("Timed out waiting for ChatGPT response to finish.")

    def handle_rate_limit(self, wait_seconds: int = 0) -> bool:
        """Detect and click dialog popup modals in ChatGPT without triggering accidental chat discards."""
        page = self.require_page()
        try:
            clicked = False
            # Search strictly within open dialogs / modal overlays
            try:
                dialogs = page.locator("[role='dialog'], div[class*='modal'], div.popover, div[data-state='open']").all()
                for dlg in dialogs:
                    if not dlg.is_visible(timeout=200):
                        continue
                    dlg_text = dlg.inner_text(timeout=200).lower()

                    # Priority 1: Clear current chat popup
                    if "clear current chat" in dlg_text or "discarded" in dlg_text:
                        clear_btn = dlg.locator("button:has-text('Clear chat'), button:has-text('Clear')").first
                        if clear_btn.count() and clear_btn.is_visible(timeout=300):
                            clear_btn.evaluate("el => el.click()")
                            clicked = True
                            time.sleep(0.4)
                            break

                    # Priority 2: Dismissive modal buttons
                    modal_close_selectors = [
                        "button[aria-label='Close']",
                        "button[aria-label='Dismiss']",
                        "[data-testid='modal-close']",
                        "button:has-text('Got it')",
                        "button:has-text('OK')",
                        "button:has-text('Dismiss')",
                        "button:has-text('Stay logged out')",
                        "button:has-text('Maybe later')",
                        "button:has-text('No thanks')",
                        "button:has-text('Not now')",
                        "button:has-text('Skip')",
                        "button:has-text('Later')",
                        "button:has-text('Continue on ChatGPT')",
                    ]
                    for sel in modal_close_selectors:
                        btn = dlg.locator(sel).first
                        if btn.count() and btn.is_visible(timeout=200):
                            btn.evaluate("el => el.click()")
                            clicked = True
                            time.sleep(0.3)
                            break
                    if clicked:
                        break
            except PlaywrightError:
                pass

            if clicked:
                print("[chatgpt] Dismissed popup modal. Continuing scraping...", flush=True)
                return True
            return False
        except Exception:
            return False

    _handle_rate_limit = handle_rate_limit


    def _dismiss_modal(self) -> None:
        """Close any blocking overlay or modal dialog using JS DOM removal."""
        page = self.require_page()

        # 0. Check and click 'Got it' or info popups inside dialogs
        self.handle_rate_limit()

        # 1. Try Escape key first
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except PlaywrightError:
            pass

        # 2. JS: search dialogs for close buttons, explicitly skip voice/start/try buttons
        try:
            page.evaluate("""
                () => {
                    const SKIP = ['voice', 'start', 'try', 'enable', 'microphone', 'record'];
                    const CLOSE = ['clear chat', 'close', 'dismiss', 'stay logged out', 'skip', 'later', 'no thanks', 'not now', 'got it', 'ok'];
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
        if self.platform_name.endswith("_anon"):
            # Anonymous guest mode intentionally has "Log in" / "Sign up" buttons in the sidebar
            return bool(re.search(r"(verify you are human|just a moment|cloudflare)", text, re.I))
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
