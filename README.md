# Keep-Alive System

[![Keep Apps Alive](https://github.com/kaushalnagrecha/wake-up-sleepy-head/actions/workflows/keep-alive.yaml/badge.svg)](https://github.com/kaushalnagrecha/wake-up-sleepy-head/actions/workflows/keep-alive.yaml)

Automated health checker that prevents Streamlit and HuggingFace Spaces from sleeping due to inactivity.

## How It Works

```
┌─────────────────────────────────────────────────────┐
│           GitHub Actions Cron (every 4h)            │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
          ┌────────────────────────┐
          │  HTTP GET each endpoint │
          └────────────┬───────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
        ┌──────────┐     ┌───────────┐
        │  AWAKE   │     │  ASLEEP / │
        │  (done)  │     │  UNCLEAR  │
        └──────────┘     └─────┬─────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Launch Selenium    │
                    │  Click wake button  │
                    │  Poll until alive   │
                    └─────────────────────┘
```

### Detection Strategy

**Step 1 — Lightweight HTTP check** (`requests`):
- Fetches the page HTML and scans for known sleep/awake markers
- Streamlit markers: `Zzzz`, `This app has gone to sleep due to inactivity`, `Yes, get this app back up!`
- HuggingFace markers: `This Space is sleeping due to inactivity`, `Restart this Space`, `"stage":"SLEEPING"`

**Step 2 — Selenium fallback** (only when asleep or inconclusive):
- Launches headless Chrome
- Clicks the platform-specific wake button
- Polls until awake markers appear (up to 120s timeout)

## Monitored Endpoints

Endpoints are stored as a simple Python list in `scripts/keep-alive.py`:

```python
ENDPOINTS: list[str] = [
    "https://kaushal-nagrecha-ama-ai.hf.space",
    "https://kn-f1-dashboard.streamlit.app/",
]
```

To add or remove endpoints, edit the `ENDPOINTS` array.

## Schedule

| Trigger | When |
|---------|------|
| Cron | Every 4 hours (`0 */4 * * *`) |
| Push | On changes to `keep-alive.py` or the workflow file on `main` |
| Manual | Via the **Actions** tab → **Keep Apps Alive** → **Run workflow** |

## File Structure

```

├── keep-alive.py          # Main health-check script
├── requirements.txt       # Python dependencies
├── .github/
│   └── workflows/
│       └── keep-alive.yml      # Keep-alive cron workflow
└── README.md               # This file
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Chrome + ChromeDriver must be installed for Selenium fallback
# On macOS: brew install --cask google-chrome chromedriver
# On Ubuntu: sudo apt install chromium-browser chromium-chromedriver

# Run the script
python keep-alive.py
```

## Logs

The script outputs structured logs to stdout. In GitHub Actions, these are visible in the job run details:

```
2026-04-05 12:00:00 [INFO] ============================================================
2026-04-05 12:00:00 [INFO] Keep-Alive Check — 2 endpoint(s)
2026-04-05 12:00:00 [INFO] ============================================================
2026-04-05 12:00:01 [INFO] Checking: https://kaushal-nagrecha-ama-ai.hf.space [platform=huggingface]
2026-04-05 12:00:02 [INFO]   → HTTP 200 — body length: 34521 chars
2026-04-05 12:00:02 [INFO]   → Found 2 sleep marker(s) — page is ASLEEP
2026-04-05 12:00:02 [INFO]   → ✗ App is ASLEEP — launching Selenium to wake it
2026-04-05 12:00:05 [INFO]   → Selenium: Found HF restart button via CSS 'form[action*='/start'] button'
2026-04-05 12:00:35 [INFO]   → Selenium: App is now AWAKE!
```
