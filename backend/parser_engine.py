"""Pluggable Discord trade signal parser supporting Gemini API, OpenAI/Codex API, CLI bridges, and fast local rules."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from backend.occ import normalize_expiry


SIGNAL_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_actionable": {"type": "boolean"},
        "action": {
            "type": "string",
            "enum": ["BTO", "TRIM", "STC", "STOP_UPDATE", "IGNORE"],
        },
        "ticker": {"type": "string"},
        "strike": {"type": "number"},
        "right": {"type": "string", "enum": ["C", "P", ""]},
        "expiry": {"type": "string"},
        "alert_price": {"type": "number"},
        "trim_pct": {"type": "number"},
        "stop_note": {"type": "string"},
    },
    "required": [
        "is_actionable",
        "action",
        "ticker",
        "strike",
        "right",
        "expiry",
        "alert_price",
        "trim_pct",
        "stop_note",
    ],
    "additionalProperties": False,
}

GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_actionable": {"type": "BOOLEAN"},
        "action": {
            "type": "STRING",
            "enum": ["BTO", "TRIM", "STC", "STOP_UPDATE", "IGNORE"],
        },
        "ticker": {"type": "STRING"},
        "strike": {"type": "NUMBER"},
        "right": {"type": "STRING"},
        "expiry": {"type": "STRING"},
        "alert_price": {"type": "NUMBER"},
        "trim_pct": {"type": "NUMBER"},
        "stop_note": {"type": "STRING"},
    },
    "required": [
        "is_actionable",
        "action",
        "ticker",
        "strike",
        "right",
        "expiry",
        "alert_price",
        "trim_pct",
        "stop_note",
    ],
}


def _build_system_prompt(open_positions: list[dict[str, Any]], today_iso: str) -> str:
    pos_summary = [
        {
            "ticker": p["ticker"],
            "contract": p["display_contract"],
            "occ": p["occ_symbol"],
            "right": p["opt_right"],
            "strike": p["strike"],
            "expiry": p["expiry_iso"],
            "qty": p["qty"],
            "caller": p.get("caller", ""),
        }
        for p in open_positions
    ]
    return (
        f"You are an institutional options order-desk signal parser. Today's date is {today_iso}.\n"
        "Convert a raw Discord message into strict JSON matching the schema.\n"
        "Rules:\n"
        "1. Actionable new option entries ('BTO', 'bought', 'in', 'starter') -> is_actionable=true, action='BTO'.\n"
        "2. Partial exits ('trim half', 'scale out 1/3', 'taking some off') -> is_actionable=true, action='TRIM', trim_pct (default 50).\n"
        "3. Full exits ('STC all', 'out rest', 'closing', 'flat', 'stopped out') -> is_actionable=true, action='STC', trim_pct=100.\n"
        "4. Stop adjustments ('stops at BE', 'move SL to 1.40') -> is_actionable=true, action='STOP_UPDATE'.\n"
        "5. Watchlist talk, conditional ideas ('watching', 'if it breaks', 'looking at'), market commentary -> is_actionable=false, action='IGNORE'.\n"
        "6. If a TRIM/STC/STOP_UPDATE omits strike/expiry/ticker, resolve it from currently open positions below.\n"
        f"Open positions: {json.dumps(pos_summary)}"
    )


def parse_with_gemini_api(
    raw_text: str,
    author: str,
    open_positions: list[dict[str, Any]],
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> dict[str, Any]:
    """Call Google Gemini REST API with strict responseSchema structured output."""
    today_iso = date.today().isoformat()
    sys_prompt = _build_system_prompt(open_positions, today_iso)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": sys_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"Caller: {author}\nDiscord message: {raw_text}"}],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_RESPONSE_SCHEMA,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    text_out = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text_out)


def parse_with_openai_api(
    raw_text: str,
    author: str,
    open_positions: list[dict[str, Any]],
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> dict[str, Any]:
    """Call OpenAI / Codex REST API with strict JSON Schema structured output."""
    today_iso = date.today().isoformat()
    sys_prompt = _build_system_prompt(open_positions, today_iso)
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Caller: {author}\nDiscord message: {raw_text}"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_signal",
                "strict": True,
                "schema": SIGNAL_JSON_SCHEMA,
            },
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    return json.loads(content)


def parse_with_cli_bridge(
    provider: str,
    raw_text: str,
    author: str,
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Optional bridge for local `codex` or `gemini` CLI binaries."""
    today_iso = date.today().isoformat()
    prompt = (
        _build_system_prompt(open_positions, today_iso)
        + "\nReturn ONLY a raw JSON object with keys: "
        "is_actionable, action, ticker, strike, right, expiry, alert_price, trim_pct, stop_note.\n"
        f"Caller: {author}\nMessage: {raw_text}"
    )
    instruction = "Parse the piped Discord trade message and output ONLY a raw JSON object."
    if provider == "CODEX_CLI":
        exe = shutil.which("codex")
        if not exe:
            raise RuntimeError("`codex` CLI binary not found on PATH. Install Codex CLI and run `codex` to sign in with ChatGPT.")
        cmd = [exe, "exec", "--skip-git-repo-check", instruction]
    else:
        exe = shutil.which("gemini")
        if not exe:
            raise RuntimeError("`gemini` CLI binary not found on PATH. Install Gemini CLI and run `gemini` to sign in with Google.")
        cmd = [exe, "-p", instruction]

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25.0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{provider} exited with {proc.returncode}")

    match = re.search(r"\{[\s\S]*\}", proc.stdout)
    if not match:
        raise ValueError("CLI output did not contain JSON")
    return json.loads(match.group(0))


STOPWORDS = {
    "BTO", "STC", "BUY", "SELL", "TRIM", "OUT", "ALL", "HALF", "REST", "HERE",
    "CALL", "CALLS", "PUT", "PUTS", "SL", "STOP", "STOPS", "BE", "AT", "IN",
    "THE", "FOR", "AND", "OR", "IF", "ON", "TO", "OF", "LOTTO", "STARTER",
    "SIZE", "SMALL", "WEEKLY", "0DTE", "1DTE", "VWAP", "HOD", "LOD", "PT", "TP",
}


def parse_with_local_rules(
    raw_text: str,
    author: str,
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sub-2ms deterministic options alert parser & fallback."""
    txt = raw_text.strip()
    lower = txt.lower()

    # Non-actionable watchlist / conditional chatter detection
    chatter_triggers = (
        "watching ", "on watch", "looking at ", "would love ", "if we break",
        "if it breaks", "wait for ", "patience", "no trades", "sitting on hands",
    )
    explicit_entry = bool(re.search(r"\b(bto|bought|buying|starter)\b", lower)) or bool(
        re.match(r"^(in|long)\s+\$?[a-z]{1,5}\b", lower)
    )
    explicit_exit = bool(re.search(r"\b(stc|trim|trimming|out\s+half|out\s+all|out\s+rest|closing|close\s+all|flat\b)", lower))
    explicit_stop = bool(re.search(r"\b(stops?\s+(at|to)|move\s+sl|sl\s+to|stop\s+at\s+be)\b", lower))

    if any(w in lower for w in chatter_triggers) and not (explicit_entry or explicit_exit):
        return {
            "is_actionable": False,
            "action": "IGNORE",
            "ticker": "",
            "strike": 0.0,
            "right": "",
            "expiry": "",
            "alert_price": 0.0,
            "trim_pct": 0.0,
            "stop_note": "",
        }

    # Extract price after '@' or 'at '
    price_match = re.search(r"(?:@\s*|\bat\s+)\$?(\d+(?:\.\d{1,4})?)", lower)
    alert_price = float(price_match.group(1)) if price_match else 0.0

    # Extract stop note if present
    stop_note = ""
    sl_match = re.search(r"\b(?:sl|stop|stops)\s*(?:@\s*|at\s+|to\s+)?(be|\$?\d+(?:\.\d{1,4})?)\b", lower)
    if sl_match:
        val = sl_match.group(1).upper().replace("$", "")
        stop_note = f"SL @ {val}"

    # Extract strike + C/P (e.g., 585C, 142.5P, 585 calls, 245 puts)
    strike = 0.0
    right = ""
    cp_compact = re.search(r"\b(\d{1,5}(?:\.\d{1,2})?)\s*([cCpP])\b", txt)
    cp_word = re.search(r"\b(\d{1,5}(?:\.\d{1,2})?)\s*(calls?|puts?)\b", lower)
    if cp_compact:
        strike = float(cp_compact.group(1))
        right = cp_compact.group(2).upper()
    elif cp_word:
        strike = float(cp_word.group(1))
        right = "C" if cp_word.group(2).startswith("c") else "P"

    # Extract expiry ('0DTE', '1DTE', '11/06', '2026-11-06')
    expiry_raw = ""
    exp_match = re.search(r"\b(0dte|1dte|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", lower)
    if exp_match:
        expiry_raw = exp_match.group(1)

    # 4-tier precision ticker extraction
    ticker = ""
    dollar_ticker = re.search(r"\$([A-Za-z]{1,5})\b", txt)
    before_contract = re.search(
        r"\b([A-Za-z]{1,5})\s+(?:(?:0dte|1dte|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+)?"
        r"\d{1,5}(?:\.\d{1,2})?\s*(?:[cCpP]|calls?|puts?)\b",
        txt,
        re.IGNORECASE,
    )
    after_verb = re.search(
        r"\b(?:bto|stc|bought|buying|trim|trimming|closing|close)\s+(?:half\s+|all\s+|rest\s+(?:of\s+)?)?\$?([A-Za-z]{1,5})\b",
        txt,
        re.IGNORECASE,
    )

    if dollar_ticker and dollar_ticker.group(1).upper() not in STOPWORDS:
        ticker = dollar_ticker.group(1).upper()
    elif before_contract and before_contract.group(1).upper() not in STOPWORDS:
        ticker = before_contract.group(1).upper()
    elif after_verb and after_verb.group(1).upper() not in STOPWORDS:
        ticker = after_verb.group(1).upper()
    else:
        for p in open_positions:
            if re.search(rf"\b{re.escape(p['ticker'])}\b", txt, re.IGNORECASE):
                ticker = p["ticker"].upper()
                break
        if not ticker:
            for tok in re.findall(r"\b[A-Z]{2,5}\b", txt):
                if tok not in STOPWORDS:
                    ticker = tok
                    break

    # Resolve action
    if explicit_stop and not explicit_entry and not explicit_exit:
        matched_pos = _match_open_pos(open_positions, ticker, author)
        return {
            "is_actionable": True,
            "action": "STOP_UPDATE",
            "ticker": ticker or (matched_pos["ticker"] if matched_pos else ""),
            "strike": strike or (float(matched_pos["strike"]) if matched_pos else 0.0),
            "right": right or (matched_pos["opt_right"] if matched_pos else ""),
            "expiry": expiry_raw or (matched_pos["expiry_iso"] if matched_pos else ""),
            "alert_price": alert_price,
            "trim_pct": 0.0,
            "stop_note": stop_note or "SL @ BE",
        }

    if explicit_exit:
        is_trim = bool(re.search(r"\b(trim|trimming|half|1/2|1/3|25%|33%|50%|scale)\b", lower)) and not bool(
            re.search(r"\b(out\s+all|out\s+rest|close\s+all|stc\s+all|flat)\b", lower)
        )
        pct = 50.0 if is_trim else 100.0
        pct_match = re.search(r"(\d{1,2})\s*%", lower)
        if is_trim and pct_match:
            pct = float(pct_match.group(1))
        elif "1/3" in lower:
            pct = 33.0
        elif "1/4" in lower:
            pct = 25.0

        matched_pos = _match_open_pos(open_positions, ticker, author)
        return {
            "is_actionable": True,
            "action": "TRIM" if is_trim else "STC",
            "ticker": ticker or (matched_pos["ticker"] if matched_pos else ""),
            "strike": strike or (float(matched_pos["strike"]) if matched_pos else 0.0),
            "right": right or (matched_pos["opt_right"] if matched_pos else ""),
            "expiry": expiry_raw or (matched_pos["expiry_iso"] if matched_pos else ""),
            "alert_price": alert_price,
            "trim_pct": pct,
            "stop_note": stop_note,
        }

    # Entry check: either explicit BTO keyword + contract, or contract + @ price
    if ticker and strike > 0 and right in ("C", "P") and (explicit_entry or alert_price > 0):
        exp_date, _ = normalize_expiry(expiry_raw or None)
        return {
            "is_actionable": True,
            "action": "BTO",
            "ticker": ticker,
            "strike": strike,
            "right": right,
            "expiry": exp_date.isoformat(),
            "alert_price": alert_price,
            "trim_pct": 0.0,
            "stop_note": stop_note,
        }

    return {
        "is_actionable": False,
        "action": "IGNORE",
        "ticker": "",
        "strike": 0.0,
        "right": "",
        "expiry": "",
        "alert_price": 0.0,
        "trim_pct": 0.0,
        "stop_note": "",
    }


def _match_open_pos(open_positions: list[dict[str, Any]], ticker: str, author: str) -> dict[str, Any] | None:
    if ticker:
        for p in open_positions:
            if p["ticker"].upper() == ticker.upper():
                return p
    if author:
        for p in open_positions:
            if p.get("caller", "").lower() == author.lower():
                return p
    return open_positions[0] if open_positions else None


def parse_discord_message(
    raw_text: str,
    author: str,
    open_positions: list[dict[str, Any]],
    settings: dict[str, Any],
) -> tuple[dict[str, Any], str, int]:
    """Parse message using configured provider, returning (parsed_dict, provider_used, latency_ms)."""
    provider = settings.get("parser_provider", "LOCAL_FAST")
    t0 = time.perf_counter()

    try:
        if provider == "GEMINI_API":
            key = settings.get("gemini_api_key", "").strip()
            if not key:
                raise ValueError("Missing Gemini API key")
            model = settings.get("gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
            res = parse_with_gemini_api(raw_text, author, open_positions, key, model)
            ms = max(1, int((time.perf_counter() - t0) * 1000))
            return res, f"GEMINI ({model})", ms

        if provider == "OPENAI_API":
            key = settings.get("openai_api_key", "").strip()
            if not key:
                raise ValueError("Missing OpenAI API key")
            model = settings.get("openai_model", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
            res = parse_with_openai_api(raw_text, author, open_positions, key, model)
            ms = max(1, int((time.perf_counter() - t0) * 1000))
            return res, f"OPENAI ({model})", ms

        if provider in ("CODEX_CLI", "GEMINI_CLI"):
            res = parse_with_cli_bridge(provider, raw_text, author, open_positions)
            ms = max(1, int((time.perf_counter() - t0) * 1000))
            return res, provider, ms

    except Exception:
        # Seamless fallback to deterministic local parser so execution never stalls
        res = parse_with_local_rules(raw_text, author, open_positions)
        ms = max(1, int((time.perf_counter() - t0) * 1000))
        return res, f"LOCAL_FALLBACK (from {provider})", ms

    res = parse_with_local_rules(raw_text, author, open_positions)
    ms = max(1, int((time.perf_counter() - t0) * 1000))
    return res, "LOCAL_FAST", ms


def test_parser_connection(settings: dict[str, Any]) -> dict[str, Any]:
    """Run a diagnostic probe against the selected parser provider."""
    provider = settings.get("parser_provider", "LOCAL_FAST")
    sample = "BTO SPY 10/30 585C @ 1.45 starter"
    t0 = time.perf_counter()
    try:
        if provider == "GEMINI_API":
            key = settings.get("gemini_api_key", "").strip()
            if not key:
                return {"ok": False, "provider": provider, "error": "Enter a Gemini API key first."}
            model = settings.get("gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
            res = parse_with_gemini_api(sample, "diag", [], key, model)
        elif provider == "OPENAI_API":
            key = settings.get("openai_api_key", "").strip()
            if not key:
                return {"ok": False, "provider": provider, "error": "Enter an OpenAI API key first."}
            model = settings.get("openai_model", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
            res = parse_with_openai_api(sample, "diag", [], key, model)
        elif provider in ("CODEX_CLI", "GEMINI_CLI"):
            res = parse_with_cli_bridge(provider, sample, "diag", [])
        else:
            res = parse_with_local_rules(sample, "diag", [])
        ms = max(1, int((time.perf_counter() - t0) * 1000))
        return {"ok": True, "provider": provider, "latency_ms": ms, "parsed": res}
    except Exception as exc:
        ms = max(1, int((time.perf_counter() - t0) * 1000))
        return {"ok": False, "provider": provider, "latency_ms": ms, "error": str(exc)}
