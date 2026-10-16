"""Turn scraper output CSVs into compact dashboard datasets.

Each project in config.json points at one scraper output CSV. Rows are grouped
into responses (one AI answer to one prompt on one platform/date), brands are
detected with the project's regexes, and ChatGPT raw payloads are mined for
fan-out search queries and pages that were retrieved but not cited.

Results are cached in dashboard/cache/ and rebuilt whenever the source CSV
changes, so the 900 MB phones file is only parsed once.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CACHE = ROOT / "cache"
CACHE_VERSION = 7

limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(limit)
        break
    except OverflowError:
        limit //= 10

SEARCH_RESULT_RE = re.compile(r'"type":\s*"search_result",\s*"url":\s*"([^"]+)"')
QUERIES_RE = re.compile(r'"type":\s*"search_model_queries",\s*"queries":\s*(\[[^\]]*\])')
# Attribution links from ChatGPT's map card, not sources.
WIDGET_HOSTS = ("mapbox.com", "openstreetmap.org")
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "srsltid"}


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def clean_url(url: str) -> str:
    """Drop tracking params (utm_source=chatgpt.com etc.) and fragments."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))


def is_widget(url: str) -> bool:
    host = domain_of(url)
    return any(host == h or host.endswith("." + h) for h in WIDGET_HOSTS)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def url_key(url: str) -> str:
    """Loose identity used to decide whether a retrieved page was also cited."""
    parts = urlparse(clean_url(url))
    return (domain_of(url) + parts.path.rstrip("/")).lower()


def mine_payload(raw: str) -> tuple[list[str], list[str]]:
    """Return (fan-out search queries, retrieved URLs) from a ChatGPT SSE capture."""
    if not raw:
        return [], []
    try:
        blob = "\n".join(p if isinstance(p, str) else json.dumps(p) for p in json.loads(raw))
    except (ValueError, TypeError):
        blob = raw
    queries: list[str] = []
    for match in QUERIES_RE.finditer(blob):
        try:
            for q in json.loads(match.group(1)):
                if isinstance(q, str) and q not in queries:
                    queries.append(q)
        except ValueError:
            continue
    retrieved = list(dict.fromkeys(clean_url(u) for u in SEARCH_RESULT_RE.findall(blob) if not is_widget(u)))
    if not retrieved and '[["wrb.fr"' in blob:
        retrieved = gemini_stream_urls(blob)
    return queries, retrieved


GOOGLE_HOST_RE = re.compile(r"(^|\.)(google\.[a-z.]+|google|gstatic\.com|googleapis\.com|googleusercontent\.com|ytimg\.com|ggpht\.com)$")


def gemini_stream_urls(blob: str) -> list[str]:
    """External URLs in a Gemini StreamGenerate capture (the pages it searched and cited)."""
    urls: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            if node.startswith("http") and not GOOGLE_HOST_RE.search(domain_of(node)):
                urls.append(clean_url(node))
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    for line in blob.splitlines():
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            inner = json.loads(line)[0][2]
            if inner:
                walk(json.loads(inner))
        except (ValueError, IndexError, TypeError):
            continue
    return list(dict.fromkeys(u for u in urls if not is_widget(u)))


def detect_brands(text: str, patterns: list[tuple[int, re.Pattern]]) -> list[list[int]]:
    """Return [[brand_index, mention_count, rank]] ordered by first mention."""
    hits = []
    for idx, pattern in patterns:
        matches = list(pattern.finditer(text))
        if matches:
            hits.append((matches[0].start(), idx, len(matches)))
    hits.sort()
    return [[idx, count, rank] for rank, (_, idx, count) in enumerate(hits, start=1)]


def brand_spec(value: str | dict) -> tuple[str, list[str]]:
    """A brand is a regex string, or {"match": regex, "domains": [its websites]}."""
    if isinstance(value, str):
        return value, []
    return value["match"], [d.lower().removeprefix("www.") for d in value.get("domains", [])]


def owner_of(domain: str, owned: list[tuple[str, int]]) -> int | None:
    for site, idx in owned:
        if domain == site or domain.endswith("." + site):
            return idx
    return None


def build_project(project: dict) -> tuple[dict, dict[str, str]]:
    source = REPO / project["source"]
    brand_names = list(project["brands"])
    specs = [brand_spec(v) for v in project["brands"].values()]
    patterns = [(i, re.compile(match, re.I)) for i, (match, _) in enumerate(specs)]
    owned = [(site, i) for i, (_, sites) in enumerate(specs) for site in sites]

    prompts: list[str] = []
    prompt_idx: dict[str, int] = {}
    urls: list[dict] = []
    url_idx: dict[str, int] = {}
    responses: dict[str, dict] = {}
    texts: dict[str, str] = {}
    last_for_key: dict[tuple[str, str, str], str] = {}

    with source.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            prompt = (row.get("prompt") or "").strip()
            if not prompt:
                continue
            platform = (row.get("platform") or "unknown").strip()
            date = (row.get("response_date") or "").strip()[:10]
            text = row.get("response_content") or ""
            key = (prompt, platform, date)
            # The scraper writes the answer text on the first row only; the
            # citation rows after it belong to that same answer.
            if not text.strip() and key in last_for_key:
                rid = last_for_key[key]
            else:
                rid = hashlib.sha1(f"{prompt}\x00{platform}\x00{date}\x00{text}".encode()).hexdigest()[:16]
            last_for_key[key] = rid

            resp = responses.get(rid)
            if resp is None:
                if prompt not in prompt_idx:
                    prompt_idx[prompt] = len(prompts)
                    prompts.append(prompt)
                resp = responses[rid] = {
                    "id": rid,
                    "p": prompt_idx[prompt],
                    "pl": platform,
                    "d": date,
                    "len": len(text),
                    "b": detect_brands(text, patterns),
                    "c": [],
                    "q": [],
                    "_retrieved": [],
                }
                texts[rid] = text

            raw_url = (row.get("url") or "").strip()
            if raw_url and not is_widget(raw_url):
                url = clean_url(raw_url)
                if url not in url_idx:
                    url_idx[url] = len(urls)
                    dom = domain_of(url)
                    urls.append({"u": url, "dom": dom, "cat": row.get("citation_category") or "Other", "own": owner_of(dom, owned)})
                if url_idx[url] not in resp["c"]:
                    resp["c"].append(url_idx[url])

            raw_payload = row.get("raw_payload") or ""
            if raw_payload and not resp["q"] and not resp["_retrieved"]:
                resp["q"], resp["_retrieved"] = mine_payload(raw_payload)

    for resp in responses.values():
        retrieved = resp.pop("_retrieved")
        cited = {url_key(urls[i]["u"]) for i in resp["c"]}
        resp["rc"] = len(retrieved)
        resp["rn"] = [u for u in retrieved if url_key(u) not in cited][:40]

    dataset = {
        "id": project["id"],
        "name": project["name"],
        "source": project["source"],
        "own_brand": project.get("own_brand"),
        "brands": brand_names,
        "prompts": prompts,
        "urls": urls,
        "responses": sorted(responses.values(), key=lambda r: (r["d"], r["p"], r["pl"])),
    }
    return dataset, texts


def source_signature(project: dict) -> str:
    source = REPO / project["source"]
    stat = source.stat()
    brands = json.dumps(project["brands"], sort_keys=True)
    return f"v{CACHE_VERSION}:{stat.st_size}:{int(stat.st_mtime)}:{hashlib.sha1(brands.encode()).hexdigest()[:8]}"


def text_db() -> sqlite3.Connection:
    CACHE.mkdir(exist_ok=True)
    conn = sqlite3.connect(CACHE / "texts.db", check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS texts (project TEXT, id TEXT, body TEXT, PRIMARY KEY (project, id))")
    return conn


def get_dataset(project: dict, conn: sqlite3.Connection) -> dict:
    """Return the cached dataset for a project, rebuilding it if the source changed."""
    CACHE.mkdir(exist_ok=True)
    cache_file = CACHE / f"{project['id']}.json"
    signature = source_signature(project)
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("signature") == signature:
            return cached

    print(f"[dashboard] building {project['id']} from {project['source']} ...", flush=True)
    dataset, texts = build_project(project)
    dataset["signature"] = signature
    with conn:
        conn.execute("DELETE FROM texts WHERE project = ?", (project["id"],))
        conn.executemany(
            "INSERT INTO texts VALUES (?, ?, ?)",
            ((project["id"], rid, body) for rid, body in texts.items()),
        )
    cache_file.write_text(json.dumps(dataset, separators=(",", ":")), encoding="utf-8")
    print(f"[dashboard] {project['id']}: {len(dataset['responses'])} responses, {len(dataset['urls'])} urls", flush=True)
    return dataset


def get_text(conn: sqlite3.Connection, project_id: str, response_id: str) -> str | None:
    row = conn.execute("SELECT body FROM texts WHERE project = ? AND id = ?", (project_id, response_id)).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    connection = text_db()
    for proj in load_config()["projects"]:
        if (REPO / proj["source"]).exists():
            get_dataset(proj, connection)
