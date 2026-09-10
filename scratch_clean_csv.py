import csv
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

input_file = Path("output.csv")
backup_file = Path("output_backup_before_clean.csv")

if not input_file.exists():
    print("output.csv not found!")
    sys.exit(1)

# Error patterns indicating a failed / blocked prompt or home screen text
error_patterns = [
    "what’s on your mind today?",
    "what's on your mind today?",
    "good to see you",
    "ready when you are",
    "what’s on the agenda today?",
    "what's on the agenda today?",
    "something went wrong",
    "we're doing a quick check",
    "we’re doing a quick check",
    "try again after",
    "anonymous message limit",
    "message limit reached",
    "too many requests",
]

total_rows_read = 0
valid_rows = []
invalid_rows = []

platforms = defaultdict(lambda: {
    "total_rows": 0,
    "unique_prompts": set(),
    "valid_rows": 0,
    "valid_prompts": set(),
    "invalid_rows": 0,
    "invalid_prompts": set()
})

fieldnames = []

with open(input_file, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        total_rows_read += 1
        p = row.get("platform", "").strip().lower()
        prompt = row.get("prompt", "").strip()
        url = row.get("url", "").strip()
        content = row.get("response_content", "").strip()

        stat = platforms[p]
        stat["total_rows"] += 1
        stat["unique_prompts"].add(prompt)

        is_invalid = False
        # If there's no URL and content matches error patterns or is too short
        if not url:
            if not content:
                is_invalid = True
            elif any(err in content.lower() for err in error_patterns):
                is_invalid = True
            elif len(content) < 50:
                is_invalid = True

        if is_invalid:
            stat["invalid_rows"] += 1
            stat["invalid_prompts"].add(prompt)
            invalid_rows.append(row)
        else:
            stat["valid_rows"] += 1
            stat["valid_prompts"].add(prompt)
            valid_rows.append(row)

print("=" * 90)
print(f"{'Platform':<15} | {'Total Rows':<10} | {'Valid Rows':<10} | {'Invalid Rows':<12} | {'Valid Prompts':<13} | {'Invalid Prompts':<15}")
print("=" * 90)
for p, s in sorted(platforms.items()):
    val_pr = len(s["valid_prompts"])
    inval_pr = len(s["invalid_prompts"] - s["valid_prompts"])
    print(f"{p:<15} | {s['total_rows']:<10} | {s['valid_rows']:<10} | {s['invalid_rows']:<12} | {val_pr:<13} | {inval_pr:<15}")
print("=" * 90)

# Check total unique prompts in prompts.csv
prompts_file = Path("prompts.csv")
total_input_prompts = 0
if prompts_file.exists():
    with open(prompts_file, "r", encoding="utf-8-sig") as pf:
        preader = csv.DictReader(pf)
        total_input_prompts = len([r for r in preader if r.get("prompt", "").strip()])

print(f"\nTotal Prompts in prompts.csv: {total_input_prompts}")
total_valid_unique_prompts = len({(r.get('platform','').strip().lower(), r.get('prompt','').strip()) for r in valid_rows})
print(f"Total Unique Valid (Platform, Prompt) pairs across all platforms: {total_valid_unique_prompts}")

# Make backup and write cleaned file
print(f"\nBacking up current output.csv to {backup_file}...")
input_file.rename(backup_file)

print(f"Writing {len(valid_rows)} cleaned rows to output.csv...")
with open(input_file, "w", encoding="utf-8-sig", newline="") as out_f:
    writer = csv.DictWriter(out_f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(valid_rows)

print("Successfully cleaned output.csv!")
