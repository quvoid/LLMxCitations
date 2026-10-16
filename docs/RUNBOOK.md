# LLMxCitations runbook

How to run AI-citation scrapes (ChatGPT + Gemini), watch them, check the data, and
produce the dashboard and Excel report. Written so another person or AI agent
(Antigravity, Cursor, etc.) can do the whole job end to end.

---

## 0. Prompt to give the agent

Paste this to the agent, then point it at this file:

```text
You are operating the LLMxCitations project in /home/omkar/Desktop/LLMxCitations.
Read docs/RUNBOOK.md fully before doing anything and follow it step by step.

Task: run the prompts in <PROMPTS_FILE> on both ChatGPT and Gemini, in parallel,
2 workers per platform. Start the dashboard, keep checking both runs until they
finish, re-run anything that failed, verify sources against the platforms' own
Sources panels on 2 prompts each, then export the Excel report.

Rules:
- Never run the scraper with more than 2 workers per platform.
- Never delete or overwrite an output CSV without first copying it to <name>_backup.csv.
- Never commit output CSVs, Excel reports, logs, browser_profiles/ or auth_state/.
- If a login, CAPTCHA or "verify you are human" screen appears, STOP and ask me.
  Do not try to solve or bypass it.
- Report progress every ~30 minutes: answers saved per platform, failures, rate limits.
- If something does not match this runbook, stop and tell me instead of improvising.
```

---

## 1. One-time setup

```bash
cd /home/omkar/Desktop/LLMxCitations
pip install -r requirements.txt
python3 -m playwright install chromium
sudo apt-get install -y xvfb      # virtual display, see section 2
```

ChatGPT runs in a **logged-in** browser profile (`browser_profiles/chatgpt*`).
Logging in must be done once by a human with a visible browser:

```bash
python3 main.py --save-auth chatgpt
```

Then, in that ChatGPT window: **Settings -> Personalization -> turn OFF "Memory"
and "Reference chat history"**. Otherwise answers are personalised by past
scraping chats and the results are not representative.

Gemini runs logged out; no setup needed.

---

## 2. If the agent cannot open a visible (headful) browser

The scraper runs Chrome headful by default because ChatGPT blocks headless
browsers more often. If your environment has no screen (Antigravity, SSH, CI),
run it inside a **virtual display** with `xvfb-run`. The browser still runs
headful, just on an invisible screen:

```bash
xvfb-run -a python3 main.py --platforms gemini --workers 2 ...
```

- **Gemini** also works with `--headless` if xvfb is unavailable.
- **ChatGPT** should use `xvfb-run`, not `--headless`.
- The ChatGPT login itself (section 1) still needs a real screen once.
  After that the saved profile is reused.

Every command below can be prefixed with `xvfb-run -a` on a headless machine.

---

## 3. Prepare the prompts

The input is a CSV with one column named `prompt`:

```csv
prompt
Best phone under 10000 in India
best phone under 10000 with good camera
```

From an Excel sheet (e.g. a column "Prompt" on sheet "All Prompts"):

```bash
python3 - <<'EOF'
from openpyxl import load_workbook
import csv
ws = load_workbook("/path/to/Prompt_List.xlsx", read_only=True)["All Prompts"]
rows = list(ws.iter_rows(values_only=True))
col = rows[0].index("Prompt")
prompts = list(dict.fromkeys(r[col].strip() for r in rows[1:] if r[col]))
with open("myproject_prompts.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["prompt"]); w.writerows([[p] for p in prompts])
print(len(prompts), "prompts")
EOF
```

---

## 4. Add the project to the dashboard

Edit `dashboard/config.json` and add an entry at the top of `"projects"`:

```json
{
  "id": "myproject",
  "name": "My Project",
  "source": ["myproject_chatgpt.csv", "myproject_gemini.csv"],
  "own_brand": "Motorola",
  "brands": {
    "Motorola": {"match": "motorola|\\bmoto\\b", "domains": ["motorola.com", "motorola.in"]},
    "Samsung":  {"match": "samsung|\\bgalaxy\\b", "domains": ["samsung.com"]}
  }
}
```

- `source` can be one CSV or a list (one per platform).
- `match` is a case-insensitive regex for mentions in the answer text.
- `domains` are the brand's own websites, used for "whose websites get cited".
- After the first answers come in, read a few and add any brands that are
  mentioned a lot but missing from the list.

---

## 5. Start the dashboard

```bash
python3 dashboard/server.py          # http://127.0.0.1:8765
```

Open http://127.0.0.1:8765 and pick the project. It reads the CSVs live:
reload the page to see new answers. Tabs: Overview (brand leaderboard,
cited websites, top domains), Prompts (click one to see every platform's
answer and sources), Sources (every cited domain).

---

## 6. Run both platforms in parallel

Use **separate output files per platform** (two processes must never write
the same CSV) and **2 workers per platform**:

```bash
nohup python3 main.py --platforms chatgpt --workers 2 \
  --input myproject_prompts.csv --output myproject_chatgpt.csv \
  --errors myproject_errors_chatgpt.log > run_chatgpt.log 2>&1 &

nohup python3 main.py --platforms gemini --workers 2 \
  --input myproject_prompts.csv --output myproject_gemini.csv \
  --errors myproject_errors_gemini.log > run_gemini.log 2>&1 &
```

What the scraper does for each prompt:

- **ChatGPT:** opens a fresh chat, sends the prompt, waits for the answer, then
  fetches the finished conversation from inside the page to get every cited
  page and every page it read. If ChatGPT rate-limits (HTTP 429), it backs off
  and retries; if the sources still cannot be read, the prompt is marked failed
  (not saved) so the next run retries it.
- **Gemini:** opens a new chat, sends the prompt unmodified, and reads the
  answer stream, which contains the full Sources panel (every quote row) and
  the unique cited pages.

Expected speed with 2 workers: roughly 1 prompt per 30-60 s per platform.

---

## 7. Keep checking the runs

Check every 15-30 minutes:

```bash
grep -c saved run_chatgpt.log; grep -c saved run_gemini.log     # answers saved
grep -E "failed|Rate limit|verification|Traceback" run_*.log | tail
ps -eo args | grep "[p]ython3 main.py"                          # still running?
```

| What you see | What it means | What to do |
|---|---|---|
| `saved N citation URL(s)` | Normal | Nothing |
| `saved 0 citation URL(s)` now and then | The AI answered without searching the web (common for "X vs Y" prompts) | Nothing |
| `failed prompt: Timed out ...` | One slow answer | Nothing now; re-run at the end |
| `Rate limit detected ... waiting 3 minutes` | Platform is throttling | Fine once in a while. If it repeats every few prompts, stop and restart with `--workers 1` |
| `ChatGPT sources unavailable` | Account rate-limited (429) | Stop ChatGPT for 1-2 hours, then resume |
| login / verify-you-are-human screen | Session expired or bot check | **Stop and ask a human.** Do not bypass |

**Stopping a run** (safe: finished answers stay in the CSV):

```bash
for p in $(ps -eo pid,args | grep "[p]ython3 main.py --platforms chatgpt" | awk '{print $1}'); do kill $p; done
```

(The `[p]` in the pattern stops the command from matching itself.)

**Resuming / re-running failures:** run the exact same command again. The
scraper skips prompts already in the output CSV and only runs the rest.

---

## 8. Check the data is complete

After the runs, check that no answers are missing sources:

```bash
python3 - <<'EOF'
import csv, sys, collections
sys.path.insert(0, "dashboard"); import data
csv.field_size_limit(10**9)
for f in ["myproject_chatgpt.csv", "myproject_gemini.csv"]:
    rows = list(csv.DictReader(open(f, encoding="utf-8-sig")))
    by = collections.defaultdict(list)
    for r in rows: by[r["prompt"]].append(r)
    none = [p for p, rs in by.items() if not any(r["url"] for r in rs)]
    print(f, "answers:", len(by), "with no cited sources:", len(none))
EOF
```

A few answers with no sources is normal (no web search). If it is more than
~10%, something is wrong: check the logs for rate limits.

**Spot-check against the platforms' own Sources panels** (do 2 prompts per
platform):

- ChatGPT: under the answer click **... (More actions) -> View sources**. The
  panel's "Web" list should all appear in our data for that prompt
  (Dashboard -> Prompts -> click the prompt).
- Gemini: under the answer click **(Show more options) -> View sources**.
  Each panel row is one quote; the pages should match our cited list.

---

## 9. Export the Excel report

```bash
python3 dashboard/export_excel.py myproject --out myproject_ai_visibility.xlsx
```

Sheets: Summary, Brand leaderboard, Brand websites cited, Top domains,
Prompt summary, Sources (per prompt), Source list (unique), Raw data
(full answer + cited URLs + pages read per prompt). Plain formatting, no colours.

---

## 10. Things that must not be committed

Output CSVs and Excel reports contain client results and raw platform data;
`browser_profiles/` and `auth_state/` contain the logged-in session.
Commit only code, `dashboard/config.json`, prompt lists and docs.

```bash
git add <code and docs only>
git commit -m "..."
git push origin main
```

---

## 11. Known pitfalls (already fixed, do not undo)

- ChatGPT must start a **fresh chat per prompt**; asking prompts as follow-ups
  in one chat changes the answers.
- ChatGPT's answer stream now stays open after the answer finishes, so sources
  come from fetching the finished conversation, not from the stream.
- Gemini prompts must be sent **unmodified** (no "please cite sources" suffix).
- Gemini citation chips are buttons, not links; cited pages come from the
  answer stream (`dashboard/data.py: gemini_citations`).
- More than 2 workers per platform from one IP/account triggers rate limits.
