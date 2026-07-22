# AI Citation URL Scraper

This project scrapes citation/source URLs from AI chat web UIs using Python and Playwright.

V1 fully implements Perplexity. Gemini, ChatGPT, and Grok are stubbed with the same interface for later implementation.

## Install

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

## Input

Create `prompts.csv` with one column named `prompt`:

```csv
prompt
What are the best budget phones in India right now?
Compare Motorola Edge 70 Pro with Samsung Galaxy S24.
```

## Run Perplexity

Perplexity is no-login by default:

```powershell
python .\main.py --input .\prompts.csv --output .\output.csv
```

The browser runs headful by default. Use `--headless` only if you are sure the target works reliably in your environment.

If a platform shows Cloudflare/human verification, complete it manually in the opened browser window. The scraper waits for the prompt box to appear and continues afterward. It does not use CAPTCHA solving or bypass tooling.

Each platform uses a persistent browser profile by default:

```text
.\browser_profiles\perplexity
.\browser_profiles\gemini
.\browser_profiles\chatgpt
```

This behaves more like a normal browser profile than a cookies-only JSON file, so manual verification is more likely to stick across runs.

Results are appended incrementally to `output.csv`:

```csv
prompt,platform,url,response_date
```

Failures are logged to `errors.log` with timestamp, platform, prompt, and exception details.

## Auth State

The scraper saves reusable Playwright storage state in:

```text
.\auth_state\{platform}.json
```

It also keeps full browser profile data in:

```text
.\browser_profiles\{platform}
```

Perplexity does not require login, but you can still save an optional logged-in session:

```powershell
python .\main.py --save-auth perplexity
```

The browser opens. Log in or complete human verification manually, then return to the terminal and press Enter. The scraper will save:

```text
.\auth_state\perplexity.json
```

Future runs automatically load that file if it exists.

## Platforms

Default:

```powershell
python .\main.py --platforms perplexity
```

Registry is already prepared for:

```powershell
python .\main.py --platforms perplexity,gemini,chatgpt,grok
```

Only Perplexity works in V1. The others raise `NotImplementedError` and are present as extension points.

## Timing

Between prompts, the runner waits a random delay between 3 and 8 seconds:

```powershell
python .\main.py --min-delay 4 --max-delay 10
```

No stealth plugins, fingerprint spoofing, CAPTCHA solving, or anti-detection evasion are used.

---

## Setting Up on a New PC (Full Process)

### 1. Prerequisites

Install the following before cloning:

- **Python 3.10+** → https://www.python.org/downloads/ *(check "Add to PATH" during install)*
- **Git** → https://git-scm.com/downloads
- **Google Chrome** *(must be installed — the scraper uses your real Chrome to avoid bot detection)*

---

### 2. Clone the Repository

```powershell
git clone https://github.com/quvoid/LLMxCitations.git
cd LLMxCitations
```

---

### 3. Install Python Dependencies

```powershell
pip install -r requirements.txt
```

---

### 4. Install Playwright Browser

```powershell
python -m playwright install chromium
```

> Note: Even though we use Google Chrome at runtime, Playwright needs Chromium installed for its driver.

---

### 5. Prepare Your Prompts File

Create a `prompts.csv` file in the project folder:

```csv
prompt
BKT tyres price list India
Balkrishna Industries tyre review
Best off road tyres for tractors
```

---

### 6. Authenticate Each Platform (One-Time Setup)

Each platform needs a saved login session. Run these one at a time:

**ChatGPT:**
```powershell
python main.py --save-auth chatgpt
```
Log in to ChatGPT in the Chrome window that opens. Once logged in and the chat box is visible, press **Enter** in the terminal.

**Gemini:**
```powershell
python main.py --save-auth gemini
```
Log in with your Google account. Once the Gemini chat page is ready, press **Enter**.

**Perplexity:**
```powershell
python main.py --save-auth perplexity
```
Log in to Perplexity. Once the Ask box is visible, press **Enter**.

Auth sessions are saved to:
```text
.\auth_state\chatgpt.json
.\auth_state\gemini.json
.\auth_state\perplexity.json
```
And persistent browser profiles to:
```text
.\browser_profiles\chatgpt\
.\browser_profiles\gemini\
.\browser_profiles\perplexity\
```

---

### 7. Run the Scraper

Run all platforms:
```powershell
python main.py --platforms chatgpt,gemini,perplexity
```

Run a single platform:
```powershell
python main.py --platforms chatgpt
```

Results are saved to `output.csv`. Errors are logged to `errors.log`.

---

### 8. Rate Limits (Built-In)

The scraper automatically enforces minimum delays between prompts per platform:

| Platform | Minimum Delay |
|---|---|
| ChatGPT | 20 seconds |
| Perplexity | 12 seconds |
| Gemini | 3 seconds (default) |

You can increase delays:
```powershell
python main.py --platforms chatgpt --min-delay 30 --max-delay 45
```

---

### 9. Delete a Profile (Re-Auth)

If a session expires or login fails, delete the profile and re-auth:

**All profiles:**
```powershell
Get-Process chrome, python -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item -Recurse -Force "browser_profiles" -ErrorAction SilentlyContinue; Remove-Item -Force "auth_state\*.json" -ErrorAction SilentlyContinue; Write-Host "Done"
```

**Perplexity profile:**
```powershell
Get-Process chrome, python -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item -Recurse -Force "browser_profiles\perplexity" -ErrorAction SilentlyContinue; Remove-Item -Force "auth_state\perplexity.json" -ErrorAction SilentlyContinue; Write-Host "Done"
```

**Gemini profile:**
```powershell
Get-Process chrome, python -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item -Recurse -Force "browser_profiles\gemini" -ErrorAction SilentlyContinue; Remove-Item -Force "auth_state\gemini.json" -ErrorAction SilentlyContinue; Write-Host "Done"
```

**ChatGPT profile:**
```powershell
Get-Process chrome, python -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item -Recurse -Force "browser_profiles\chatgpt" -ErrorAction SilentlyContinue; Remove-Item -Force "auth_state\chatgpt.json" -ErrorAction SilentlyContinue; Write-Host "Done"
```

---

### Output CSV Columns

| Column | Description |
|---|---|
| `prompt` | The question asked |
| `platform` | chatgpt / gemini / perplexity |
| `url` | Citation URL found in the response |
| `citation_category` | Category of the URL (News, Blog, etc.) |
| `response_date` | Date the prompt was run |
| `response_content` | Full text of the LLM response |
| `bkt_mentions` | Count of "BKT" mentions in response |
| `bkt_tyres_mentions` | Count of "BKT Tyres" mentions |
| `balkrishna_industries_limited_mentions` | Count of full brand name mentions |


