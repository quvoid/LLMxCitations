from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from playwright.sync_api import Browser, BrowserContext, Page


class PlatformScraper(ABC):
    """Common interface for AI chat citation scrapers."""

    platform_name: str
    start_url: str

    def __init__(
        self,
        browser: Browser | None = None,
        auth_dir: str | Path = "auth_state",
        context: BrowserContext | None = None,
    ) -> None:
        self.browser = browser
        self.auth_dir = Path(auth_dir)
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.storage_state_path = self.auth_dir / f"{self.platform_name}.json"
        self.context: BrowserContext | None = context
        self.page: Page | None = None

    def __enter__(self) -> "PlatformScraper":
        if self.context is None:
            self.context = self._new_context()
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.save_storage_state()
        if self.context:
            self.context.close()

    def _new_context(self) -> BrowserContext:
        if self.browser is None:
            raise RuntimeError("Browser is required when no persistent context is provided.")
        kwargs = {
            "viewport": {"width": 1440, "height": 1000},
            "locale": "en-US",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        }
        if self.storage_state_path.exists():
            kwargs["storage_state"] = str(self.storage_state_path)
        return self.browser.new_context(**kwargs)

    def require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("Scraper page is not initialized. Use it as a context manager.")
        return self.page

    def save_storage_state(self) -> None:
        if self.context is not None:
            self.context.storage_state(path=str(self.storage_state_path))

    @abstractmethod
    def login_if_needed(self) -> None:
        """Navigate and prepare the platform. May be a no-op for public platforms."""

    @abstractmethod
    def submit_prompt(self, prompt: str) -> None:
        """Submit prompt and block until the response has finished generating."""

    @abstractmethod
    def get_citation_urls(self) -> list[str]:
        """Return cited/source URLs from the current response."""

    def get_response_text(self) -> str:
        """Return the full text of the current AI response. Override in subclasses."""
        return ""


class NotImplementedPlatformScraper(PlatformScraper):
    """Stub for future platform implementations."""

    def login_if_needed(self) -> None:
        raise NotImplementedError(f"{self.platform_name} scraper is not implemented yet.")

    def submit_prompt(self, prompt: str) -> None:
        raise NotImplementedError(f"{self.platform_name} scraper is not implemented yet.")

    def get_citation_urls(self) -> list[str]:
        raise NotImplementedError(f"{self.platform_name} scraper is not implemented yet.")


def dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
