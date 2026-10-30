# AutoTrader — Options Signal Execution Terminal

Local Windows options signal parser, slippage guardrail engine, and multi-mode execution workbench supporting **Co-Pilot (Robinhood Manual Assist)**, **Paper Auto (Alpaca Sandbox)**, and **Live Auto (Broker API)**.

## Quick Start

Requires Python 3.10+ (zero external `pip` packages required; uses Python standard library + native Edge/Chrome `--app` desktop shell).

1. Optional: copy `.env.example` to `.env` to store local API credentials (`.env` and `*.db` are git-ignored).
2. Launch the desktop terminal:

```powershell
python app.py
```

Or double-click `start_autotrader.bat`.

## Connecting Gemini or OpenAI / Codex

Click **Connections** (or **Parse Latency** in the top utility bar) to select your active Signal Parser:

1. **Google Gemini API (`GEMINI_API` — Recommended for low latency, ~250–450ms)**
   - Enter your `GEMINI_API_KEY` from Google AI Studio (`gemini-2.5-flash` default).
   - Uses strict `responseSchema` JSON structured output with live open-position context.
2. **OpenAI / Codex API (`OPENAI_API`, ~300–500ms)**
   - Enter your `OPENAI_API_KEY` (`gpt-4.1-mini` default; also supports `gpt-4o-mini`, `codex-mini-latest`).
   - Uses strict `json_schema` structured outputs.
3. **Codex CLI (`CODEX_CLI`) or Gemini CLI (`GEMINI_CLI`)**
   - Routes through locally authenticated `codex exec` or `gemini -p` binaries if installed on `PATH`.
   - Note: CLI process startup adds ~2–4s per message; direct API keys are recommended for live 0DTE/weekly options.
4. **Local Deterministic Engine (`LOCAL_FAST`, <2ms)**
   - Active out-of-the-box and automatically serves as a fallback if an external API call times out.

## Keyboard Shortcuts (Co-Pilot Ticket)

- `C` or `Space`: Copy option contract & OCC symbol to clipboard
- `Enter`: Log manual Robinhood fill into the Open Positions Ledger (`RH-MANUAL`)
- `Esc`: Skip current pending ticket / dismiss open modal

