#!/usr/bin/env python3
"""
Motorola mentions scraper.

Reads URLs from a .txt file, fetches readable article markdown through Jina
Reader, ignores link-only/navigation/spec-table noise, extracts phone model
mentions from headings, article body, and content-card style blocks, then writes
a CSV.

Optional Hugging Face path:
  pip install transformers torch

The script still works without those packages by using the deterministic regex
extractor.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Iterable


DEFAULT_INPUT = "urls.txt"
DEFAULT_OUTPUT = "motorola_mentions.csv"
USER_AGENT = "MotoMentionsScraper/1.0"


PHONE_BRANDS = [
    "Motorola",
    "Moto",
    "Samsung Galaxy",
    "Apple iPhone",
    "iPhone",
    "OnePlus Nord",
    "OnePlus",
    "Nothing Phone",
    "Google Pixel",
    "Xiaomi",
    "Redmi Note",
    "Redmi",
    "POCO",
    "Realme",
    "iQOO",
    "Vivo",
    "OPPO",
    "Nokia",
    "Sony Xperia",
    "Asus ROG Phone",
    "Asus Zenfone",
    "Honor",
    "Huawei",
    "Tecno",
    "Infinix",
    "Lava",
    "Lenovo",
    "Nubia",
    "Fairphone",
]

COMPETITOR_BRANDS = [b for b in PHONE_BRANDS if b not in {"Motorola", "Moto"}]

KEEP_UPPER = {
    "AI",
    "AMOLED",
    "CE",
    "FE",
    "GT",
    "HDR",
    "LTE",
    "NFC",
    "OIS",
    "POCO",
    "ROG",
    "SE",
    "UFS",
    "USB",
    "XL",
    "5G",
    "4G",
}

BAD_FIRST_TOKEN = re.compile(
    r"^(ram|storage|battery|display|camera|processor|chipset|price|review|"
    r"specs|features|nits|mah|hz|mp|gb|tb|ghz|watt|inch|screen|sensor|"
    r"design|build|color|colour|variant|version|edition|series|lineup|range|"
    r"model|phone|device|smartphone|mobile|update|launch|sale|deal|offer|"
    r"brand|logo|image|photo|video|popular|trending|latest|related|also|"
    r"tags|share|buy|get|view|see|check|visit|explore|find|browse|other|"
    r"more|all|top|best|new|upcoming|recently|officially|confirmed|announced|"
    r"leaked|rumoured|rumored|expected|snapdragon|dimensity|helio|exynos|"
    r"tensor|qualcomm|mediatek|unisoc|score|rating|add|compare|cooling|"
    r"lpddr|ufs|has|have|had|already|comes|come|gets|got)$",
    re.I,
)

STOP_WORDS_AFTER_MODEL = re.compile(
    r"\s+(review|price|buy|under|best|vs|and|or|with|for|in|at|is|are|was|"
    r"the|a|an|its|this|that|launch|launched|available|india|specs|features|"
    r"camera|battery|display|performance|design|verdict|pros|cons|rating|"
    r"score|announced|release|released|confirmed|tipped|leaked|rumoured|"
    r"rumored|expected|coming|soon|series|lineup|range|phones|devices|"
    r"smartphones|sale|discount|offer|deal|popular|top|gets|packs|offers|"
    r"delivers|costs|priced|powered|runs|ships|sports|brings|beats|beat|"
    r"competes|compete|rivals|rival|renders|render|leaks|leak|will|may|could)\b.*$",
    re.I,
)


@dataclass
class TextBlock:
    section: str
    text: str


class ArticleHTMLParser(HTMLParser):
    ARTICLE_HINT = re.compile(r"(article|content|post|entry|news|review|story|card)", re.I)
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "header"}
    TEXT_TAGS = {"title", "h1", "h2", "h3", "h4", "p", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.link_depth = 0
        self.article_depth = 0
        self.current_tag = ""
        self.current_parts: list[str] = []
        self.lines: list[str] = []
        self.meta_lines: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if tag == "a":
            self.link_depth += 1
        identity = " ".join([attrs_dict.get("id", ""), attrs_dict.get("class", ""), attrs_dict.get("itemprop", "")])
        if tag in {"article", "main", "section", "div"} and self.ARTICLE_HINT.search(identity):
            self.article_depth += 1
        if tag == "meta":
            name = attrs_dict.get("name") or attrs_dict.get("property") or attrs_dict.get("itemprop")
            content = attrs_dict.get("content")
            if name and content and re.search(r"(published|date|modified|time)", name, re.I):
                self.meta_lines.append(f"{name}: {content}")
        if tag in self.TEXT_TAGS:
            self.flush_current()
            self.current_tag = tag
            self.current_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.TEXT_TAGS:
            self.flush_current(prefix="#" if tag.startswith("h") or tag == "title" else "")
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == "a" and self.link_depth:
            self.link_depth -= 1
        if tag in {"article", "main", "section", "div"} and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth or self.link_depth or not self.current_tag:
            return
        if self.current_tag == "title" or self.article_depth or self.current_tag.startswith("h"):
            self.current_parts.append(data)

    def flush_current(self, prefix: str = "") -> None:
        if not self.current_parts:
            self.current_tag = ""
            return
        text = clean_cell(" ".join(self.current_parts))
        if text:
            self.lines.append((prefix + " " + text).strip())
        self.current_parts = []
        self.current_tag = ""


def html_to_markdownish(html: str) -> str:
    parser = ArticleHTMLParser()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.meta_lines + parser.lines)


def direct_fetch_url(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,text/plain",
            "User-Agent": "Mozilla/5.0 (compatible; MotoMentionsScraper/1.0)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"Direct fetch returned HTTP {status}")
        html = response.read().decode("utf-8", errors="replace")
    return html_to_markdownish(html)


def fetch_url(url: str, timeout: int = 25, jina_api_key: str = "") -> str:
    jina_url = "https://r.jina.ai/" + url.strip()
    headers = {
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
        "X-Timeout": str(min(timeout, 20)),
        "User-Agent": USER_AGENT,
    }
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"
    request = urllib.request.Request(
        jina_url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"Jina returned HTTP {status}")
        markdown = response.read().decode("utf-8", errors="replace")
    if len(markdown) < 800 and re.search(r"\blogo\b|small image|cached snapshot", markdown, re.I):
        return direct_fetch_url(url, timeout=timeout)
    return markdown


def read_urls(path: str) -> list[str]:
    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def extract_published_date(markdown: str) -> str:
    patterns = [
        r"published[\s\-_]*time[:\s]+([^\n]+)",
        r"published[\s\-_]*date[:\s]+([^\n]+)",
        r"date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"updated[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2} [A-Za-z]+ \d{4})",
        r"([A-Za-z]+ \d{1,2},?\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, markdown, re.I)
        if match:
            return clean_cell(match.group(1))
    return ""


def remove_hyperlinks(line: str) -> str:
    # Drop whole lines that are only links. Inline links lose the URL but keep
    # surrounding sentence text.
    if re.fullmatch(r"\s*\[.*?]\(https?://[^)]+\)\s*", line):
        return ""
    line = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", line)
    line = re.sub(r"\[[^\]]*]\(https?://[^)]+\)", " ", line)
    line = re.sub(r"https?://\S+", " ", line)
    return line


def is_noise_line(line: str) -> bool:
    t = line.strip()
    if not t:
        return True
    if re.match(r"^#{1,4}\s+", t):
        return False
    if re.match(r"^\d+[\.)]\s+[A-Z]", t):
        return False
    if re.match(r"^\+\s*(compare|add|remove)", t, re.I):
        return True
    if re.match(r"^vs\.?$", t, re.I):
        return True
    if re.match(
        r"^(Display|Processor|RAM|Storage|Battery Capacity|Rear Camera|"
        r"Front Camera|Chipset|OS|Connectivity|Sensors?|Colors?|Weight|"
        r"Dimensions?|Build|SIM|Network|Bluetooth|Wi-?Fi|GPS|NFC|USB|Audio|"
        r"Charging|Resolution|Refresh Rate|Brightness|Protection)\s*:?\s*$",
        t,
        re.I,
    ):
        return True
    if re.match(r"^[\d,.\s]+(nits|mah|hz|mp|gb|tb|ghz|watts?|inch|cm|pixels?)\b", t, re.I):
        return True
    if re.match(r"^[₹$€£¥]\s?[\d,]+\s*$", t):
        return True
    if re.match(r"^(qualcomm\s+snapdragon|mediatek\s+dimensity|mediatek\s+helio|unisoc|exynos|google\s+tensor)", t, re.I):
        return True
    if re.match(r"^(View Photo Gallery|View All|See All|Read More|Buy Now|Check Price|Full Specs|Know More|Learn More|Explore More|Add to Compare|Launched:)", t, re.I):
        return True
    if re.match(r"^\d+(\.\d+)?\s*/\s*\d+\s*\([\d,]+\s*ratings?\)", t, re.I):
        return True
    has_brand = re.search(
        r"\b(motorola|moto|samsung|apple|iphone|oneplus|realme|xiaomi|redmi|"
        r"poco|iqoo|vivo|oppo|nokia|google pixel|nothing phone|honor|huawei|"
        r"tecno|infinix|lava|lenovo|nubia|fairphone|asus)\b",
        t,
        re.I,
    )
    if len(t) < 15 and not has_brand:
        return True
    return False


def classify_line(line: str) -> str:
    t = line.strip()
    if re.match(r"^#{1,4}\s+", t):
        return "header"
    if re.match(r"^[-*]\s+", t) or re.match(r"^\d+[\.)]\s+", t):
        return "card"
    if len(t) <= 140 and re.search(r"\b(price|deal|launch|review|vs|best|top|under|specs|features)\b", t, re.I):
        return "card"
    return "body"


def extract_blocks(markdown: str) -> list[TextBlock]:
    body_start = re.search(r"^#{1,2}\s+", markdown, re.M)
    if body_start and body_start.start() > 0:
        markdown = markdown[body_start.start() :]

    footer_patterns = [
        r"^#+\s*(trending|popular now|latest news|related articles?|also read|"
        r"you may also like|recommended|more from|advertisement|sponsored|tags?|"
        r"share this|comments?|subscribe|newsletter|about the author|faqs?)\b",
        r"trending gadgets and topics",
        r"^#+\s*specifications?\s*$",
        r"^#+\s*pros (and|&) cons\s*$",
    ]
    for pattern in footer_patterns:
        match = re.search(pattern, markdown, re.I | re.M)
        if match and match.start() > 80:
            markdown = markdown[: match.start()]

    blocks: list[TextBlock] = []
    for raw_line in markdown.splitlines():
        line = remove_hyperlinks(raw_line)
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if is_noise_line(line):
            continue
        section = classify_line(line)
        text = re.sub(r"^#{1,6}\s*", "", line)
        text = re.sub(r"^[-*]\s*", "", text)
        blocks.append(TextBlock(section, text))
    return blocks


def title_case_phone(name: str) -> str:
    fixed: list[str] = []
    for word in re.split(r"(\s+)", name.strip()):
        if not word.strip():
            fixed.append(word)
            continue
        if word.upper() in KEEP_UPPER:
            fixed.append(word.upper())
        elif word.lower() == "iphone":
            fixed.append("iPhone")
        elif word.lower() == "iqoo":
            fixed.append("iQOO")
        elif word.lower() == "oneplus":
            fixed.append("OnePlus")
        elif word.lower() == "redmi":
            fixed.append("Redmi")
        else:
            fixed.append(word[:1].upper() + word[1:].lower())
    return "".join(fixed)


def normalize_phone(name: str, brand: str) -> str | None:
    name = clean_cell(name)
    name = STOP_WORDS_AFTER_MODEL.sub("", name).strip(" -:,.|/")
    if not name:
        return None
    after_brand = name[len(brand) :].strip() if name.lower().startswith(brand.lower()) else name
    if not after_brand:
        return None
    first = after_brand.split()[0]
    if BAD_FIRST_TOKEN.match(first):
        return None
    if re.search(r"\b\d+\s*(gb|tb|mp|mah|hz|ghz|nits|watt|inch)\b", name, re.I):
        return None
    if len(after_brand) > 70:
        return None
    return title_case_phone(name)


def iter_phone_matches(text: str, brands: Iterable[str]) -> Iterable[tuple[str, str]]:
    for brand in brands:
        escaped = re.escape(brand)
        pattern = re.compile(
            r"(?<![,+])\b"
            + escaped
            + r"\s+([A-Za-z0-9][A-Za-z0-9+.-]*(?:\s+[A-Za-z0-9+.-]+){0,5})",
            re.I,
        )
        for match in pattern.finditer(text):
            full = match.group(0).strip()
            normalized = normalize_phone(full, brand)
            if normalized:
                yield brand, normalized


def load_hf_ner(model_name: str):
    try:
        from transformers import pipeline
    except Exception:
        return None
    try:
        return pipeline("ner", model=model_name, aggregation_strategy="simple")
    except Exception as exc:
        print(f"HF model unavailable, using regex only: {exc}", file=sys.stderr)
        return None


def hf_candidates(text: str, ner_pipeline) -> set[str]:
    if ner_pipeline is None:
        return set()
    candidates: set[str] = set()
    # Keep chunks small because many NER models have 512-token limits.
    for chunk in [text[i : i + 1800] for i in range(0, min(len(text), 12000), 1800)]:
        try:
            entities = ner_pipeline(chunk)
        except Exception:
            continue
        for entity in entities:
            word = clean_cell(entity.get("word", ""))
            if re.search(r"\b(" + "|".join(re.escape(b) for b in PHONE_BRANDS) + r")\b", word, re.I):
                candidates.add(word)
    return candidates


def extract_mentions(blocks: list[TextBlock], ner_pipeline=None) -> dict[str, object]:
    moto_counts: Counter[str] = Counter()
    competitor_counts: Counter[str] = Counter()
    source_sections: defaultdict[str, set[str]] = defaultdict(set)
    contexts: defaultdict[str, list[str]] = defaultdict(list)
    combined_text = "\n".join(block.text for block in blocks)

    # Hugging Face helps identify entity-like spans; regex then validates model
    # shape and counts real mentions in the article text.
    candidate_text = combined_text + "\n" + "\n".join(hf_candidates(combined_text, ner_pipeline))

    for block in blocks:
        block_moto = {phone for _, phone in iter_phone_matches(block.text, ["Motorola", "Moto"])}
        block_competitors = {
            phone
            for _, phone in iter_phone_matches(block.text, COMPETITOR_BRANDS)
            if not re.search(r"\b(motorola|moto)\b", phone, re.I)
        }
        for phone in block_moto:
            moto_counts[phone] += 1
            source_sections[phone].add(block.section)
            if len(contexts[phone]) < 3:
                contexts[phone].append(block.text)
        for phone in block_competitors:
            competitor_counts[phone] += 1
            source_sections[phone].add(block.section)

    for _, phone in iter_phone_matches(candidate_text, ["Motorola", "Moto"]):
        moto_counts.setdefault(phone, 0)
    for _, phone in iter_phone_matches(candidate_text, COMPETITOR_BRANDS):
        competitor_counts.setdefault(phone, 0)

    return {
        "motorola_mentions": format_counts(moto_counts),
        "competitors": format_counts(competitor_counts),
        "source_sections": "; ".join(
            f"{phone}: {'/'.join(sorted(source_sections[phone]))}"
            for phone in sorted(source_sections)
        ),
        "mention_contexts": " || ".join(
            f"{phone}: {' | '.join(snippets)}" for phone, snippets in sorted(contexts.items())
        ),
    }


def format_counts(counter: Counter[str]) -> str:
    real_items = [(name, count) for name, count in counter.items() if count > 0]
    if not real_items:
        return "Not mentioned"
    return ", ".join(
        f"{name} x{count}" if count > 1 else name
        for name, count in sorted(real_items, key=lambda item: item[0].lower())
    )


def clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def scrape_one(url: str, ner_pipeline=None, delay: float = 0.0, jina_api_key: str = "") -> dict[str, str]:
    raw_markdown = fetch_url(url, jina_api_key=jina_api_key)
    blocks = extract_blocks(raw_markdown)
    mentions = extract_mentions(blocks, ner_pipeline)
    if delay:
        time.sleep(delay)
    return {
        "url": url,
        "published_date": extract_published_date(raw_markdown) or "Not found",
        "motorola_mentions": mentions["motorola_mentions"],
        "all_competitor_phones_found": mentions["competitors"],
        "source_sections": mentions["source_sections"],
        "mention_contexts": mentions["mention_contexts"],
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ok",
        "error": "",
    }


def error_row(url: str, exc: Exception) -> dict[str, str]:
    return {
        "url": url,
        "published_date": "",
        "motorola_mentions": "Error",
        "all_competitor_phones_found": "",
        "source_sections": "",
        "mention_contexts": "",
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "status": "error",
        "error": str(exc),
    }


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    fields = [
        "url",
        "published_date",
        "motorola_mentions",
        "all_competitor_phones_found",
        "source_sections",
        "mention_contexts",
        "scraped_at",
        "status",
        "error",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Motorola phone mentions from URL list into CSV.")
    parser.add_argument("--input", "-i", default=DEFAULT_INPUT, help="Text file with one URL per line.")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="CSV output path.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between URLs in seconds.")
    parser.add_argument(
        "--hf-model",
        default="dslim/bert-base-NER",
        help="Optional Hugging Face NER model name. Use --no-hf to disable.",
    )
    parser.add_argument("--no-hf", action="store_true", help="Disable Hugging Face and use regex only.")
    parser.add_argument(
        "--jina-api-key",
        default=os.environ.get("JINA_API_KEY", ""),
        help="Jina Reader API key. Defaults to the JINA_API_KEY environment variable.",
    )
    parser.add_argument("--json-debug", action="store_true", help="Print rows as JSON after writing CSV.")
    args = parser.parse_args()

    urls = read_urls(args.input)
    if not urls:
        print(f"No URLs found in {args.input}", file=sys.stderr)
        return 1

    ner_pipeline = None if args.no_hf else load_hf_ner(args.hf_model)
    rows: list[dict[str, str]] = []
    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{len(urls)}] Scraping {url}")
        try:
            rows.append(
                scrape_one(
                    url,
                    ner_pipeline=ner_pipeline,
                    delay=args.delay,
                    jina_api_key=args.jina_api_key,
                )
            )
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            rows.append(error_row(url, exc))
        except Exception as exc:
            rows.append(error_row(url, exc))

    write_csv(args.output, rows)
    print(f"Wrote {len(rows)} rows to {args.output}")
    if args.json_debug:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
