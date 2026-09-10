from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import multiprocessing
import queue
import random
import re
import sys
import threading

import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# Increase CSV field size limit for large raw payloads
max_csv_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(max_csv_limit)
        break
    except OverflowError:
        max_csv_limit = int(max_csv_limit / 10)


from chatgpt import ChatGPTScraper
from gemini import GeminiScraper
from perplexity import PerplexityScraper


class ChatGPTAnonScraper(ChatGPTScraper):
    platform_name = "chatgpt_anon"

    def save_storage_state(self) -> None:
        # Do not save any storage state or cookies for anonymous logged-out scraper
        pass


PLATFORMS = {
    "perplexity": PerplexityScraper,
    "gemini": GeminiScraper,
    "chatgpt": ChatGPTScraper,
    "chatgpt_anon": ChatGPTAnonScraper,
}



try:
    from grok import GrokScraper
    PLATFORMS["grok"] = GrokScraper
except ImportError:
    pass


CSV_LOCK = threading.Lock()


OUTPUT_FIELDS = [
    "prompt",
    "platform",
    "url",
    "citation_category",
    "response_date",
    "response_content",
    "motorola_mentions",
    "samsung_mentions",
    "apple_mentions",
    "xiaomi_mentions",
    "oneplus_mentions",
    "vivo_mentions",
    "oppo_mentions",
    "realme_mentions",
    "google_mentions",
    "nokia_mentions",
    "poco_mentions",
    "nothing_mentions",
    "lava_mentions",
    "infinix_mentions",
    "tecno_mentions",
    "raw_payload",
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
    with CSV_LOCK:
        if output_path.exists() and output_path.stat().st_size > 0:
            # Check if the header matches current OUTPUT_FIELDS; rewrite header if stale
            with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
                existing_fields = next(csv.reader(handle), [])
            if existing_fields != OUTPUT_FIELDS:
                # Read all rows, rewrite file with updated header preserving old data
                with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
                    rows = list(csv.DictReader(handle))
    if output_path.exists() and output_path.stat().st_size > 0:
        with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
            existing_fields = next(csv.reader(handle), [])
        if existing_fields != OUTPUT_FIELDS:
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


def get_completed_prompts(output_path_str: str) -> set[tuple[str, str]]:
    """Return set of (platform, prompt) tuples already in output CSV."""
    output_path = Path(output_path_str)
    completed = set()
    if output_path.exists() and output_path.stat().st_size > 0:
        with open(output_path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                p = row.get("platform", "").strip().lower()
                prompt_txt = row.get("prompt", "").strip()
                if p and prompt_txt:
                    completed.add((p, prompt_txt))
    return completed


def append_rows(path: str, rows: list[dict[str, str]], lock: multiprocessing.Lock | None = None) -> None:
    if not rows:
        return
    if lock:
        with lock:
            with open(path, "a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
                writer.writerows(rows)
                handle.flush()
    else:
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
        raise ValueError("--save-auth accepts only a single platform name at a time.")
    platform_name = platform_names[0]
    scraper_cls = PLATFORMS[platform_name]

    print(f"Opening browser for manual login on {platform_name}...")
    with sync_playwright() as playwright:
        context = launch_platform_context(playwright, platform_name, args, headless=False)
        with scraper_cls(context=context, auth_dir=args.auth_dir) as scraper:
            scraper.login_if_needed()
            print("Press Enter in this terminal once you are logged in and ready to save auth state...")
            input()
            scraper.save_storage_state()
            cookies, origins = count_storage_state(scraper.storage_state_path)
            print(f"Saved auth state to {scraper.storage_state_path} ({cookies} cookies, {origins} origins)")
            if cookies == 0 and origins == 0:
                print("Warning: saved state is empty. Login/session probably was not captured.")
    return 0


def count_storage_state(path: Path) -> tuple[int, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return len(data.get("cookies", [])), len(data.get("origins", []))

def launch_platform_context(playwright, platform_name: str, args, headless: bool | None = None, worker_id: int = 0):
    profile_name = f"{platform_name}_worker_{worker_id}" if worker_id > 0 else platform_name
    profile_dir = Path(args.profile_dir) / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    is_anon = platform_name.endswith("_anon")

    auth_platform_name = platform_name.replace("_anon", "")
    auth_state_path = Path(args.auth_dir) / f"{auth_platform_name}.json"

    if is_anon:
        # Wipe anonymous profile directory before launch to ensure 100% clean guest state
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
    else:
        # If worker profile doesn't exist yet, clone from base platform profile
        base_profile = Path(args.profile_dir) / platform_name
        if worker_id > 0 and base_profile.exists() and not (profile_dir / "Default").exists():
            try:
                import shutil
                shutil.copytree(base_profile, profile_dir, dirs_exist_ok=True)
            except Exception:
                pass

        # Automatically clean up any leftover Chrome singleton lock files from previous interrupted runs
        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            target_lock = profile_dir / lock_name
            if target_lock.is_symlink() or target_lock.exists():
                try:
                    target_lock.unlink()
                except OSError:
                    pass

    chrome_args = [
        "--disable-blink-features=AutomationControlled",
    ]
    if is_anon:
        chrome_args.append("--incognito")

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
        "args": chrome_args,
    }

    try:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            **launch_kwargs
        )
    except Exception as exc:
        print(f"Could not launch system Chrome ({exc}). Falling back to default Chromium...", file=sys.stderr)
        context = playwright.chromium.launch_persistent_context(
            **launch_kwargs
        )

    try:
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
        """)
    except Exception:
        pass

    if is_anon:
        try:
            context.clear_cookies()
        except Exception:
            pass

    elif auth_state_path.exists():
        try:
            auth_data = json.loads(auth_state_path.read_text(encoding="utf-8"))
            cookies = auth_data.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
        except Exception as e:
            print(f"Could not inject saved auth cookies into context: {e}", file=sys.stderr)

    return context





def process_worker(
    worker_id: int,
    platform_name: str,
    scraper_cls,
    prompt_queue: multiprocessing.Queue,
    args,
    total_prompts: int,
    csv_lock: multiprocessing.Lock,
):
    # Stagger worker initialization slightly to prevent simultaneous window server contention
    time.sleep((worker_id - 1) * 1.5)

    with sync_playwright() as playwright:
        context = None
        scraper = None

        def recreate_scraper():
            nonlocal context, scraper
            try:
                if scraper:
                    if scraper.context:
                        scraper.context.close()
            except Exception:
                pass
            context = launch_platform_context(playwright, platform_name, args, worker_id=worker_id)
            scraper = scraper_cls(context=context, auth_dir=args.auth_dir)
            scraper.__enter__()
            scraper.login_if_needed()
            return scraper

        try:
            recreate_scraper()
        except Exception as exc:
            logging.exception("Worker %d platform=%s setup error=%s", worker_id, platform_name, exc)
            print(f"[Worker-{worker_id}] [{platform_name}] setup failed: {exc}. Retrying in 2s...", file=sys.stderr)
            time.sleep(2.0)
            try:
                recreate_scraper()
            except Exception as e2:
                print(f"[Worker-{worker_id}] [{platform_name}] fatal setup error: {e2}", file=sys.stderr)
                return

        while True:
            try:
                item = prompt_queue.get()
                if item is None:
                    break
                index, prompt = item
            except Exception:
                break

            print(f"[Worker-{worker_id}] [{platform_name}] ({index}/{total_prompts}) Processing: {prompt[:60]}...", flush=True)
            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                try:
                    if not scraper or not scraper.page or scraper.page.is_closed():
                        recreate_scraper()

                    scraper.submit_prompt(prompt)
                    urls = scraper.get_citation_urls()
                    response_text = scraper.get_response_text()

                    # Brand mention counters for mobile phone brands
                    m_counts = {
                        "motorola_mentions": len(re.findall(r"\b(?:motorola|moto)\b", response_text, re.I)),
                        "samsung_mentions": len(re.findall(r"\b(?:samsung|galaxy)\b", response_text, re.I)),
                        "apple_mentions": len(re.findall(r"\b(?:apple|iphone|ipad)\b", response_text, re.I)),
                        "xiaomi_mentions": len(re.findall(r"\b(?:xiaomi|redmi|mi)\b", response_text, re.I)),
                        "oneplus_mentions": len(re.findall(r"\b(?:oneplus|1plus)\b", response_text, re.I)),
                        "vivo_mentions": len(re.findall(r"\b(?:vivo|iqoo)\b", response_text, re.I)),
                        "oppo_mentions": len(re.findall(r"\b(?:oppo)\b", response_text, re.I)),
                        "realme_mentions": len(re.findall(r"\b(?:realme)\b", response_text, re.I)),
                        "google_mentions": len(re.findall(r"\b(?:google|pixel)\b", response_text, re.I)),
                        "nokia_mentions": len(re.findall(r"\b(?:nokia)\b", response_text, re.I)),
                        "poco_mentions": len(re.findall(r"\b(?:poco)\b", response_text, re.I)),
                        "nothing_mentions": len(re.findall(r"\b(?:nothing)\b", response_text, re.I)),
                        "lava_mentions": len(re.findall(r"\b(?:lava)\b", response_text, re.I)),
                        "infinix_mentions": len(re.findall(r"\b(?:infinix)\b", response_text, re.I)),
                        "tecno_mentions": len(re.findall(r"\b(?:tecno)\b", response_text, re.I)),
                    }
                    today = date.today().isoformat()

                    # Extract captured network response raw payload
                    raw_payload = ""
                    if hasattr(scraper, "captured_network_responses") and scraper.captured_network_responses:
                        raw_payload = json.dumps([item.get("body", "") for item in scraper.captured_network_responses])
                        scraper.captured_network_responses.clear()

                    base_data = {
                        "prompt": prompt,
                        "platform": platform_name,
                        "response_date": today,
                        "raw_payload": raw_payload,
                    }

                    # Validate that response is not empty or an error screen
                    error_indicators = [
                        "something went wrong",
                        "we're doing a quick check",
                        "we’re doing a quick check",
                        "what’s on your mind today?",
                        "what's on your mind today?",
                        "good to see you",
                        "ready when you are",
                    ]
                    if not urls and (not response_text or any(err in response_text.lower() for err in error_indicators) or len(response_text.strip()) < 40):
                        raise RuntimeError(f"Empty or error response received from {platform_name}: {response_text[:80]!r}")

                    if urls:
                        rows = []
                        for i, url in enumerate(urls):
                            row = {
                                **base_data,
                                "url": url,
                                "citation_category": categorize_url(url),
                                "response_content": response_text if i == 0 else "",
                            }
                            for b_col, b_val in m_counts.items():
                                row[b_col] = b_val if i == 0 else ""
                            row["raw_payload"] = raw_payload if i == 0 else ""
                            rows.append(row)
                    else:
                        row = {
                            **base_data,
                            "url": "",
                            "citation_category": "",
                            "response_content": response_text,
                        }
                        for b_col, b_val in m_counts.items():
                            row[b_col] = b_val
                        rows = [row]

                    append_rows(args.output, rows, lock=csv_lock)
                    scraper.save_storage_state()
                    print(f"[Worker-{worker_id}] [{platform_name}] saved {len(urls)} citation URL(s) for prompt", flush=True)
                    break

                except Exception as exc:
                    if ("closed" in str(exc).lower() or "connection" in str(exc).lower() or "target" in str(exc).lower()) and attempt < max_attempts:
                        print(f"[Worker-{worker_id}] [{platform_name}] Browser disconnected. Auto-recovering fresh browser...", file=sys.stderr)
                        try:
                            recreate_scraper()
                        except Exception:
                            pass
                        continue

                    if "Rate limited" in str(exc) and attempt < max_attempts:
                        print(f"[Worker-{worker_id}] [{platform_name}] Rate limited on attempt {attempt}. Retrying prompt after 3-minute wait...", file=sys.stderr)
                        time.sleep(180)
                        continue

                    logging.exception("Worker %d platform=%s prompt=%r error=%s", worker_id, platform_name, prompt, exc)
                    print(f"[Worker-{worker_id}] [{platform_name}] failed prompt: {exc}", file=sys.stderr)
                    try:
                        recreate_scraper()
                    except Exception:
                        pass
                    break

            platform_min = getattr(scraper, "RATE_LIMIT_DELAY", 0.0)
            lo = max(args.min_delay, platform_min)
            hi = max(args.max_delay, lo)
            delay = random.uniform(lo, hi)
            time.sleep(delay)

        try:
            if scraper and scraper.context:
                scraper.context.close()
        except Exception:
            pass



def run_scrape(args) -> int:
    prompts = read_prompts(args.input)
    if not prompts:
        print(f"No prompts found in {args.input}", file=sys.stderr)
        return 1

    platforms = parse_platforms(args.platforms)
    ensure_output(args.output)
    setup_logging(args.errors)

    completed = set() if args.no_resume else get_completed_prompts(args.output)

    for platform_name in platforms:
        scraper_cls = PLATFORMS[platform_name]
        
        remaining = []
        for idx, prompt in enumerate(prompts, start=1):
            if (platform_name, prompt) in completed:
                continue
            remaining.append((idx, prompt))


        if not remaining:
            print(f"[{platform_name}] All {len(prompts)} prompts are already completed in {args.output}. Skipping.")
            continue

        print(f"[{platform_name}] Starting {len(remaining)} remaining prompt(s) out of {len(prompts)} total (Workers: {args.workers}).")

        prompt_queue = multiprocessing.Queue()
        for item in remaining:
            prompt_queue.put(item)

        num_workers = min(args.workers, len(remaining))
        # Add termination sentinels for each worker process
        for _ in range(num_workers):
            prompt_queue.put(None)

        csv_lock = multiprocessing.Lock()
        processes = []
        for worker_id in range(1, num_workers + 1):
            p = multiprocessing.Process(
                target=process_worker,
                args=(worker_id, platform_name, scraper_cls, prompt_queue, args, len(prompts), csv_lock),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

    print("Scrape completed successfully.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape citation URLs from AI chat platforms (Perplexity, Gemini, ChatGPT, ChatGPT_Anon)."
    )
    parser.add_argument(
        "--platforms",
        default="perplexity,gemini,chatgpt",
        help="Comma-separated platform names or 'all'. Available: perplexity, gemini, chatgpt, chatgpt_anon (default: perplexity,gemini,chatgpt).",
    )
    parser.add_argument("--input", default="prompts.csv", help="Path to input prompts CSV file.")
    parser.add_argument("--output", default="output.csv", help="Path to output CSV file.")
    parser.add_argument("--errors", default="errors.log", help="Path to error log file.")
    parser.add_argument("--auth-dir", default="auth_state", help="Directory where auth state files are stored.")
    parser.add_argument(
        "--profile-dir",
        default="browser_profiles",
        help="Directory for persistent browser profiles (used for login and scraping).",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Slow down Playwright actions by milliseconds.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent browser workers to use per platform (default: 1).",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable resume capability; re-scrape all prompts from scratch.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=5.0,
        help="Minimum delay in seconds between consecutive prompts (default: 5.0).",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=15.0,
        help="Maximum delay in seconds between consecutive prompts (default: 15.0).",
    )
    parser.add_argument(
        "--save-auth",
        metavar="PLATFORM",
        help="Open browser for manual login and save auth state for PLATFORM, then exit.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("--max-delay must be greater than or equal to --min-delay.")

    if args.save_auth:
        return run_manual_auth(args)
    return run_scrape(args)


if __name__ == "__main__":
    raise SystemExit(main())

