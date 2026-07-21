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
