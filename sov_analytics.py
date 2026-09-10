from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path



# Increase CSV field size limit for large raw payloads
max_csv_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(max_csv_limit)
        break
    except OverflowError:
        max_csv_limit = int(max_csv_limit / 10)

BRAND_PATTERNS = {
    "motorola_mentions": ("Motorola / Moto", r"\b(?:motorola|moto)\b"),
    "samsung_mentions": ("Samsung", r"\b(?:samsung|galaxy)\b"),
    "apple_mentions": ("Apple / iPhone", r"\b(?:apple|iphone|ipad)\b"),
    "xiaomi_mentions": ("Xiaomi / Redmi", r"\b(?:xiaomi|redmi|hyperos|mi)\b"),
    "oneplus_mentions": ("OnePlus", r"\b(?:oneplus|oxygenos|1plus)\b"),
    "vivo_mentions": ("Vivo / iQOO", r"\b(?:vivo|iqoo)\b"),
    "oppo_mentions": ("Oppo", r"\b(?:oppo|coloros)\b"),
    "realme_mentions": ("Realme", r"\b(?:realme)\b"),
    "google_mentions": ("Google Pixel", r"\b(?:google|pixel)\b"),
    "nokia_mentions": ("Nokia", r"\b(?:nokia)\b"),
    "poco_mentions": ("Poco", r"\b(?:poco)\b"),
    "nothing_mentions": ("Nothing", r"\b(?:nothing)\b"),
    "lava_mentions": ("Lava", r"\b(?:lava)\b"),
    "infinix_mentions": ("Infinix", r"\b(?:infinix)\b"),
    "tecno_mentions": ("Tecno", r"\b(?:tecno)\b"),
}


def parse_int(val: str | None) -> int:
    if not val:
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def calculate_analytics(csv_path: str | Path, target_platform: str | None = None) -> dict:
    path = Path(csv_path)
    if not path.exists() or path.stat().st_size == 0:
        return {
            "total_rows": 0,
            "unique_prompts": 0,
            "total_citations": 0,
            "total_mentions": 0,
            "brands": {},
            "recent_prompts": [],
        }

    total_rows = 0
    total_citations = 0
    unique_prompts = set()

    # Per-brand accumulators
    brand_mentions = defaultdict(int)
    brand_citations = defaultdict(int)
    brand_prompts_seen = defaultdict(set)
    brand_rank1_count = defaultdict(int)

    # Prompt-level aggregation: prompt -> {brand: mentions}
    prompt_mentions = defaultdict(lambda: defaultdict(int))
    prompt_citations_count = defaultdict(int)
    recent_prompt_list = []

    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                platform = row.get("platform", "").strip().lower()
                if target_platform and platform != target_platform.lower():
                    continue

                total_rows += 1
                prompt_txt = row.get("prompt", "").strip()
                url = row.get("url", "").strip()
                response_text = row.get("response_content", "").strip()


                if url:
                    total_citations += 1

                if not prompt_txt:
                    continue

                prompt_key = (platform, prompt_txt)
                if prompt_key not in unique_prompts:
                    unique_prompts.add(prompt_key)
                    recent_prompt_list.append((platform, prompt_txt))

                if url:
                    prompt_citations_count[prompt_key] += 1

                text_to_scan = f"{response_text} {prompt_txt}"

                # Accumulate brand mentions from columns or response_content regex search
                for col_name, (brand_label, pattern) in BRAND_PATTERNS.items():
                    m_count = parse_int(row.get(col_name))
                    if m_count == 0 and text_to_scan.strip():
                        m_count = len(re.findall(pattern, text_to_scan, re.I))

                    if m_count > 0:
                        brand_mentions[brand_label] += m_count
                        brand_prompts_seen[brand_label].add(prompt_key)
                        prompt_mentions[prompt_key][brand_label] += m_count
                        if url:
                            brand_citations[brand_label] += 1
    except Exception as exc:
        print(f"Error reading {csv_path}: {exc}", file=sys.stderr)


    sum_all_mentions = sum(brand_mentions.values())

    # Calculate rank #1 wins per prompt
    for prompt_key, mentions_map in prompt_mentions.items():
        if not mentions_map:
            continue
        max_m = max(mentions_map.values())
        if max_m > 0:
            winners = [b for b, count in mentions_map.items() if count == max_m]
            for w in winners:
                brand_rank1_count[w] += 1

    # Format brand statistics
    brand_stats = []
    for col_name, (brand_label, pattern) in BRAND_PATTERNS.items():
        m_count = brand_mentions[brand_label]

        c_count = brand_citations[brand_label]
        p_count = len(brand_prompts_seen[brand_label])
        r1_wins = brand_rank1_count[brand_label]
        sov_pct = (m_count / sum_all_mentions * 100.0) if sum_all_mentions > 0 else 0.0
        presence_pct = (p_count / len(unique_prompts) * 100.0) if unique_prompts else 0.0

        brand_stats.append({
            "brand": brand_label,
            "mentions": m_count,
            "sov_percent": sov_pct,
            "citations": c_count,
            "prompts_present": p_count,
            "presence_percent": presence_pct,
            "rank1_wins": r1_wins,
        })

    # Sort leaderboard by total mentions descending
    brand_stats.sort(key=lambda x: (x["mentions"], x["citations"]), reverse=True)
    for rank, b in enumerate(brand_stats, start=1):
        b["rank"] = rank

    # Format recent prompt rankings
    recent_rankings = []
    for platform, prompt_txt in reversed(recent_prompt_list[-5:]):
        prompt_key = (platform, prompt_txt)
        mentions_map = prompt_mentions[prompt_key]
        ranked_brands = sorted(
            [{"brand": b, "mentions": count} for b, count in mentions_map.items() if count > 0],
            key=lambda x: x["mentions"],
            reverse=True,
        )
        top_brand_name = ranked_brands[0]["brand"] if ranked_brands else "None"
        top_mentions = ranked_brands[0]["mentions"] if ranked_brands else 0

        recent_rankings.append({
            "platform": platform,
            "prompt": prompt_txt,
            "citations_count": prompt_citations_count[prompt_key],
            "top_brand": top_brand_name,
            "top_mentions": top_mentions,
            "rankings": ranked_brands,
        })

    return {
        "total_rows": total_rows,
        "unique_prompts": len(unique_prompts),
        "total_citations": total_citations,
        "total_mentions": sum_all_mentions,
        "brands": brand_stats,
        "recent_prompts": recent_rankings,
    }


def render_dashboard(data: dict, csv_path: str) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f"  📊 REAL-TIME SHARE OF VOICE (SOV) & BRAND CITATION DASHBOARD")
    lines.append(f"  Source File: {csv_path} | Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)

    lines.append(
        f" Total Prompts Processed : {data['unique_prompts']:<10} | "
        f"Total Citation URLs : {data['total_citations']:<10}"
    )
    lines.append(
        f" Total Brand Mentions   : {data['total_mentions']:<10} | "
        f"Total Output Rows   : {data['total_rows']:<10}"
    )
    lines.append("-" * 80)
    lines.append(" 🏆 OVERALL BRAND LEADERBOARD & SHARE OF VOICE (SOV)")
    lines.append("-" * 80)
    lines.append(
        f" {'Rank':<5} | {'Brand Name':<22} | {'Mentions':<9} | {'SOV %':<8} | {'Citations':<10} | {'Prompts %':<10} | {'#1 Wins':<7}"
    )
    lines.append("-" * 80)

    if not data["brands"]:
        lines.append("  No brand data recorded yet. Waiting for prompts...")
    else:
        for b in data["brands"]:
            lines.append(
                f" #{b['rank']:<4} | {b['brand']:<22} | {b['mentions']:<9} | {b['sov_percent']:<7.2f}% | "
                f"{b['citations']:<10} | {b['presence_percent']:<9.1f}% | {b['rank1_wins']:<7}"
            )

    lines.append("-" * 80)
    lines.append(" 📌 RECENT PROMPTS BRAND RANKINGS (Last 5 Prompts)")
    lines.append("-" * 80)

    if not data["recent_prompts"]:
        lines.append("  No prompts processed yet.")
    else:
        for item in data["recent_prompts"]:
            prompt_snip = item["prompt"][:55] + "..." if len(item["prompt"]) > 55 else item["prompt"]
            lines.append(f" [{item['platform'].upper()}] Prompt: \"{prompt_snip}\"")
            lines.append(
                f"   └─ Top Brand: {item['top_brand']} ({item['top_mentions']} mentions) | Citations: {item['citations_count']}"
            )
            if item["rankings"]:
                rank_str = ", ".join([f"#{r+1} {b['brand']} ({b['mentions']})" for r, b in enumerate(item["rankings"])])
                lines.append(f"   └─ Full Rank: {rank_str}")
            lines.append("")

    lines.append("=" * 80)
    lines.append(" Press Ctrl+C to exit monitoring mode.")
    return "\n".join(lines)


def export_csv_report(data: dict, export_path: str) -> None:
    with open(export_path, "w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(
            h,
            fieldnames=["rank", "brand", "mentions", "sov_percent", "citations", "prompts_present", "presence_percent", "rank1_wins"],
        )
        writer.writeheader()
        writer.writerows(data["brands"])
    print(f"Exported summary report to {export_path}")


def export_json_report(data: dict, export_path: str) -> None:
    with open(export_path, "w", encoding="utf-8") as h:
        json.dump(data, h, indent=2)
    print(f"Exported JSON analytics report to {export_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Share of Voice (SOV) & Brand Citation Analytics CLI")
    parser.add_argument("--input", default="output.csv", help="Input CSV output file path. Default: output.csv.")
    parser.add_argument("--platform", help="Filter analytics by platform (e.g. chatgpt, chatgpt_anon).")
    parser.add_argument("--watch", action="store_true", default=True, help="Enable live auto-refresh monitor mode (default).")
    parser.add_argument("--interval", type=float, default=3.0, help="Refresh interval in seconds for --watch. Default: 3.0.")
    parser.add_argument("--once", action="store_true", help="Run once and exit instead of live monitoring loop.")

    parser.add_argument("--export-csv", help="Export brand leaderboard summary to CSV file.")
    parser.add_argument("--export-json", help="Export full analytics report to JSON file.")
    args = parser.parse_args()

    if args.once:
        data = calculate_analytics(args.input, target_platform=args.platform)
        print(render_dashboard(data, args.input))
        if args.export_csv:
            export_csv_report(data, args.export_csv)
        if args.export_json:
            export_json_report(data, args.export_json)
        return 0

    try:
        while True:
            data = calculate_analytics(args.input, target_platform=args.platform)
            os.system("cls" if os.name == "nt" else "clear")
            print(render_dashboard(data, args.input))


            if args.export_csv:
                export_csv_report(data, args.export_csv)
            if args.export_json:
                export_json_report(data, args.export_json)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nExited monitoring loop.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
