from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from chatgpt import ChatGPTScraper
from gemini import GeminiScraper
from grok import GrokScraper
from perplexity import PerplexityScraper


PLATFORMS = {
    "perplexity": PerplexityScraper,
    "gemini": GeminiScraper,
    "chatgpt": ChatGPTScraper,
    "grok": GrokScraper,
}

OUTPUT_FIELDS = [
    "prompt",
    "platform",
    "url",
    "citation_category",
    "response_date",
    "response_content",
    "meningococcal_mentions",
    "bkt_mentions",
    "bkt_tyres_mentions",
    "balkrishna_industries_limited_mentions",
]


# ── URL category rules (checked top-to-bottom, first match wins) ─────────────
_CATEGORY_RULES: list[tuple[re.Pattern, str]] = [
    # Video
    (re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com)", re.I), "Video"),
    # Social Media
    (re.compile(r"(reddit\.com|twitter\.com|x\.com|facebook\.com|instagram\.com"
                r"|linkedin\.com|pinterest\.com|threads\.net|snapchat\.com|t\.me)", re.I), "Social Media"),
    # Q&A / Forum
    (re.compile(r"(quora\.com|stackexchange\.com|stackoverflow\.com|answers\.yahoo\.com)", re.I), "Q&A"),
    # Wiki / Reference
    (re.compile(r"(wikipedia\.org|wikihow\.com|wikidata\.org)", re.I), "Wiki/Reference"),
    # E-Commerce
    (re.compile(r"(amazon\.|flipkart\.com|bigbasket\.com|zepto\.com|blinkit\.com"
                r"|swiggy\.com|myntra\.com|meesho\.com|nykaa\.com|snapdeal\.com"
                r"|jiomart\.com|shopify\.com|shoopy\.in|mystore\.in|store\.)", re.I), "E-Commerce"),
    # Health / Medical
    (re.compile(r"(healthline\.com|webmd\.com|mayoclinic\.org|medicalnewstoday\.com"
                r"|drugs\.com|medlineplus\.gov|nih\.gov|cdc\.gov|who\.int)", re.I), "Health/Medical"),
    # News
    (re.compile(r"(ndtv\.com|timesofindia\.com|hindustantimes\.com|thehindu\.com"
                r"|indianexpress\.com|livemint\.com|economictimes\.com|timesnownews\.com"
                r"|bbc\.com|cnn\.com|reuters\.com|theguardian\.com|forbes\.com"
                r"|businessinsider\.com|moneycontrol\.com|financialexpress\.com)", re.I), "News"),
    # Blog platforms
    (re.compile(r"(blogspot\.com|wordpress\.com|medium\.com|substack\.com"
                r"|blogger\.com|tumblr\.com|ghost\.io)", re.I), "Blog"),
    # Review / Rating
    (re.compile(r"(trustpilot\.com|yelp\.com|glassdoor\.com|ambitionbox\.com"
                r"|justdial\.com|sulekha\.com)", re.I), "Review/Rating"),
    # Government
    (re.compile(r"\.gov(\.in)?(/|$)", re.I), "Government"),
    # Educational
    (re.compile(r"\.edu(/|$)", re.I), "Educational"),
    # Food / Recipe
    (re.compile(r"(zomato\.com|swiggy\.com|foodnetindia\.in|eatthismuch\.com"
                r"|fatsecret\.co\.in|nutriscan\.app|happycredit\.in)", re.I), "Food/Nutrition"),
    # Brand / Official  (britannia, amul, etc.) – broad catch-all for .co.in / .com brand pages
    (re.compile(r"(britannia\.co\.in|amul\.com|nestle\.in|itcportal\.com|parle\.com)", re.I), "Brand/Official"),
]


def categorize_url(url: str) -> str:
    """Return a human-readable category label for a citation URL."""
    if not url:
        return ""
    for pattern, label in _CATEGORY_RULES:
        if pattern.search(url):
            return label
    # Fallback: if URL contains blog-like path segments
    if re.search(r"/(blog|article|news|post|story|read)/", url, re.I):
        return "Blog"
    return "Website"


def setup_logging(path: str) -> None:
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s\t%(levelname)s\t%(message)s",
        encoding="utf-8",
    )


def read_prompts(path: str) -> list[str]:
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "prompt" not in reader.fieldnames:
            raise ValueError(f"{path} must contain a single column named 'prompt'.")
        return [row["prompt"].strip() for row in reader if row.get("prompt", "").strip()]


def ensure_output(path: str) -> None:
    output_path = Path(path)
    if output_path.exists() and output_path.stat().st_size > 0:
        # Check if the header matches current OUTPUT_FIELDS; rewrite header if stale
        with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
            existing_fields = next(csv.reader(handle), [])
        if existing_fields != OUTPUT_FIELDS:
            # Read all rows, rewrite file with updated header preserving old data
            with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        return
    with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()


def append_rows(path: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writerows(rows)
        handle.flush()


def parse_platforms(value: str) -> list[str]:
    if value == "all":
        return list(PLATFORMS)
    platforms = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [platform for platform in platforms if platform not in PLATFORMS]
    if unknown:
        raise ValueError(f"Unknown platform(s): {', '.join(unknown)}")
    return platforms


def run_manual_auth(args) -> int:
    platform_names = parse_platforms(args.save_auth)
    if len(platform_names) != 1:
        raise ValueError("--save-auth accepts one platform name at a time.")
    platform_name = platform_names[0]
    scraper_cls = PLATFORMS[platform_name]

    with sync_playwright() as playwright:
        context = launch_platform_context(playwright, platform_name, args, headless=False)
        with scraper_cls(context=context, auth_dir=args.auth_dir) as scraper:
            page = scraper.require_page()
            page.goto(scraper.start_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"Opened {scraper.start_url}")
            print("If you need login or verification, complete it manually in the browser.")
            print("Do not press Enter until the chat page is open and usable.")
            input()
            scraper.save_storage_state()
            cookie_count, origin_count = count_storage_state(scraper.storage_state_path)
            print(f"Saved auth state to {scraper.storage_state_path}")
            print(f"Saved cookies={cookie_count}, origins={origin_count}")
            print(f"Persistent profile: {Path(args.profile_dir) / platform_name}")
            if cookie_count == 0 and origin_count == 0:
                print("Warning: saved state is empty. Login/session probably was not captured.")
    return 0


def count_storage_state(path: Path) -> tuple[int, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return len(data.get("cookies", [])), len(data.get("origins", []))


def launch_platform_context(playwright, platform_name: str, args, headless: bool | None = None):
    profile_dir = Path(args.profile_dir) / platform_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": args.headless if headless is None else headless,
        "slow_mo": args.slow_mo,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "en-US",
        "ignore_default_args": ["--enable-automation"],
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
        "args": [
            "--disable-blink-features=AutomationControlled",
        ],
    }

    try:
        # Attempt to use real system Google Chrome to pass Cloudflare fingerprinting
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            **launch_kwargs
        )
    except Exception as exc:
        import sys
        print(f"Could not launch system Chrome ({exc}). Falling back to default Chromium...", file=sys.stderr)
        context = playwright.chromium.launch_persistent_context(
            **launch_kwargs
        )

    return context


def run_scrape(args) -> int:
    prompts = read_prompts(args.input)
    if not prompts:
        print(f"No prompts found in {args.input}", file=sys.stderr)
        return 1

    platforms = parse_platforms(args.platforms)
    ensure_output(args.output)
    setup_logging(args.errors)

    with sync_playwright() as playwright:
        for platform_name in platforms:
            scraper_cls = PLATFORMS[platform_name]
            context = launch_platform_context(playwright, platform_name, args)
            with scraper_cls(context=context, auth_dir=args.auth_dir) as scraper:
                try:
                    scraper.login_if_needed()
                except Exception as exc:
                    logging.exception("platform=%s prompt=%r error=%s", platform_name, "<login>", exc)
                    print(f"[{platform_name}] login/setup failed: {exc}", file=sys.stderr)
                    continue

                for index, prompt in enumerate(prompts, start=1):
                    print(f"[{platform_name}] {index}/{len(prompts)}")
                    try:
                        scraper.submit_prompt(prompt)
                        urls = scraper.get_citation_urls()
                        response_text = scraper.get_response_text()
                        mening_count = len(re.findall(r"meningococcal", response_text, re.I))
                        bkt_count = len(re.findall(r"\bbkt\b", response_text, re.I))
                        bkt_tyres_count = len(re.findall(r"\bbkt[- ]?tyres\b", response_text, re.I))
                        balkrishna_count = len(re.findall(r"\bbalkrishna\s+industries\s+(?:limited|ltd\.?)\b", response_text, re.I))
                        today = date.today().isoformat()

                        if urls:
                            rows = [
                                {
                                    "prompt": prompt,
                                    "platform": platform_name,
                                    "url": url,
                                    "citation_category": categorize_url(url),
                                    "response_date": today,
                                    "response_content": response_text if i == 0 else "",
                                    "meningococcal_mentions": mening_count if i == 0 else "",
                                    "bkt_mentions": bkt_count if i == 0 else "",
                                    "bkt_tyres_mentions": bkt_tyres_count if i == 0 else "",
                                    "balkrishna_industries_limited_mentions": balkrishna_count if i == 0 else "",
                                }
                                for i, url in enumerate(urls)
                            ]
                        else:
                            rows = [
                                {
                                    "prompt": prompt,
                                    "platform": platform_name,
                                    "url": "",
                                    "citation_category": "",
                                    "response_date": today,
                                    "response_content": response_text,
                                    "meningococcal_mentions": mening_count,
                                    "bkt_mentions": bkt_count,
                                    "bkt_tyres_mentions": bkt_tyres_count,
                                    "balkrishna_industries_limited_mentions": balkrishna_count,
                                }
                            ]
                        append_rows(args.output, rows)
                        scraper.save_storage_state()
                        print(f"[{platform_name}] wrote {len(rows)} citation URL(s), meningococcal_mentions={mening_count}, bkt_mentions={bkt_count}, bkt_tyres_mentions={bkt_tyres_count}, balkrishna_industries_limited_mentions={balkrishna_count}")
                    except Exception as exc:
                        logging.exception("platform=%s prompt=%r error=%s", platform_name, prompt, exc)
                        print(f"[{platform_name}] failed prompt: {exc}", file=sys.stderr)

                    if index < len(prompts):
                        delay = random.uniform(args.min_delay, args.max_delay)
                        time.sleep(delay)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape citation/source URLs from AI chat web UIs.")
    parser.add_argument("--input", default="prompts.csv", help="Input CSV with a 'prompt' column.")
    parser.add_argument("--output", default="output.csv", help="Output CSV path.")
    parser.add_argument("--errors", default="errors.log", help="Error log path.")
    parser.add_argument("--auth-dir", default="auth_state", help="Directory for Playwright storage_state JSON files.")
    parser.add_argument("--profile-dir", default="browser_profiles", help="Directory for persistent browser profiles.")
    parser.add_argument("--platforms", default="perplexity", help="Comma-separated platforms, or 'all'. Default: perplexity.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless. Default is headful.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow_mo in milliseconds.")
    parser.add_argument("--min-delay", type=float, default=3.0, help="Minimum random delay between prompts.")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Maximum random delay between prompts.")
    parser.add_argument("--save-auth", help="Open one platform and save auth_state after manual login.")
    args = parser.parse_args()

    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("--max-delay must be greater than or equal to --min-delay.")

    if args.save_auth:
        return run_manual_auth(args)
    return run_scrape(args)


if __name__ == "__main__":
    raise SystemExit(main())
