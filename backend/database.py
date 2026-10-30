"""SQLite state persistence for AutoTrader signals, orders, positions, and settings."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.occ import build_occ_symbol, normalize_expiry


DB_PATH = Path(__file__).resolve().parent.parent / "autotrader.db"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv_file(env_path: Path = ENV_PATH) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from local .env without external dependencies."""
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v:
            loaded[k] = v
            os.environ.setdefault(k, v)
    return loaded

DEFAULT_SETTINGS: dict[str, str] = {
    "execution_mode": "COPILOT",        # COPILOT | PAPER_AUTO | LIVE_AUTO
    "live_armed": "false",
    "pause_entries": "false",
    "max_slippage_pct": "5.0",
    "daily_risk_cap": "500.0",
    "max_risk_per_trade": "250.0",
    "auto_cancel_seconds": "15",
    "parser_provider": "LOCAL_FAST",    # GEMINI_API | OPENAI_API | GEMINI_CLI | CODEX_CLI | LOCAL_FAST
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "openai_api_key": "",
    "openai_model": "gpt-4.1-mini",
    "alpaca_paper_key": "",
    "alpaca_paper_secret": "",
    "alpaca_live_key": "",
    "alpaca_live_secret": "",
    "discord_channel_filter": "options-alerts",
    "discord_listener_enabled": "true",
    "last_parse_latency_ms": "18",
}


class StateDB:
    """Thread-safe SQLite store for terminal state."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_iso TEXT NOT NULL,
                    ts_ms_display TEXT NOT NULL,
                    author TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    is_actionable INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL DEFAULT 'IGNORE',
                    ticker TEXT DEFAULT '',
                    occ_symbol TEXT DEFAULT '',
                    tape_code TEXT DEFAULT '',
                    display_contract TEXT DEFAULT '',
                    rh_clipboard TEXT DEFAULT '',
                    expiry_iso TEXT DEFAULT '',
                    opt_right TEXT DEFAULT '',
                    strike REAL DEFAULT 0.0,
                    alert_price REAL DEFAULT 0.0,
                    live_bid REAL DEFAULT 0.0,
                    live_ask REAL DEFAULT 0.0,
                    slippage_pct REAL DEFAULT 0.0,
                    slippage_verdict TEXT DEFAULT 'N/A',
                    trim_pct REAL DEFAULT 0.0,
                    stop_note TEXT DEFAULT '',
                    parser_used TEXT DEFAULT 'LOCAL_FAST',
                    parse_latency_ms INTEGER DEFAULT 0,
                    ticket_status TEXT DEFAULT 'IGNORED',
                    suggested_qty INTEGER DEFAULT 1,
                    routed_origin TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occ_symbol TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    display_contract TEXT NOT NULL,
                    tape_code TEXT NOT NULL,
                    rh_clipboard TEXT NOT NULL,
                    expiry_iso TEXT NOT NULL,
                    opt_right TEXT NOT NULL,
                    strike REAL NOT NULL,
                    origin TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    initial_qty INTEGER NOT NULL,
                    avg_entry REAL NOT NULL,
                    live_bid REAL NOT NULL,
                    live_ask REAL NOT NULL,
                    stop_note TEXT DEFAULT '',
                    caller TEXT DEFAULT '',
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    realized_pnl REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    ts_display TEXT NOT NULL,
                    signal_id INTEGER DEFAULT 0,
                    side TEXT NOT NULL,
                    occ_symbol TEXT NOT NULL,
                    display_contract TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    caller_price REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    fill_price REAL DEFAULT 0.0,
                    live_ref_price REAL DEFAULT 0.0,
                    slippage_pct REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    reject_reason TEXT DEFAULT '',
                    expires_at_epoch REAL DEFAULT 0.0,
                    broker_order_id TEXT DEFAULT ''
                );
                """
            )
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            # Hydrate env / .env API keys if present and DB value is blank
            dotenv_vals = _load_dotenv_file()
            env_map = {
                "GEMINI_API_KEY": "gemini_api_key",
                "OPENAI_API_KEY": "openai_api_key",
                "ALPACA_PAPER_KEY": "alpaca_paper_key",
                "ALPACA_PAPER_SECRET": "alpaca_paper_secret",
                "ALPACA_LIVE_KEY": "alpaca_live_key",
                "ALPACA_LIVE_SECRET": "alpaca_live_secret",
            }
            for env_name, setting_key in env_map.items():
                env_val = dotenv_vals.get(env_name) or os.environ.get(env_name, "")
                if env_val:
                    cur = conn.execute("SELECT value FROM settings WHERE key=?", (setting_key,)).fetchone()
                    if cur and not cur["value"]:
                        conn.execute(
                            "UPDATE settings SET value=? WHERE key=?",
                            (env_val, setting_key),
                        )
                        if setting_key == "gemini_api_key":
                            conn.execute(
                                "UPDATE settings SET value='GEMINI_API' WHERE key='parser_provider'"
                            )

            # One-time wipe of legacy auto-seeded demo data while preserving saved API keys/settings
            migrated = conn.execute("SELECT value FROM settings WHERE key='clean_start_v2'").fetchone()
            if not migrated:
                conn.execute("DELETE FROM signals")
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM orders")
                conn.execute("INSERT INTO settings (key, value) VALUES ('clean_start_v2', 'true')")

            conn.commit()

    def _seed_if_empty(self) -> None:
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
            if count > 0:
                return

            now = datetime.now()
            exp_nvda, _ = normalize_expiry("11/06", now.date())
            exp_spy, _ = normalize_expiry("10/30", now.date())
            exp_tsla, _ = normalize_expiry("11/06", now.date())

            nvda = build_occ_symbol("NVDA", exp_nvda, "C", 140)
            spy = build_occ_symbol("SPY", exp_spy, "C", 585)
            tsla = build_occ_symbol("TSLA", exp_tsla, "P", 245)

            # Seed 2 open positions (1 RH-MANUAL, 1 ALPACA-PAPER) -> $180 + $128 risk
            conn.execute(
                """
                INSERT INTO positions (
                    occ_symbol, ticker, display_contract, tape_code, rh_clipboard,
                    expiry_iso, opt_right, strike, origin, qty, initial_qty,
                    avg_entry, live_bid, live_ask, stop_note, caller, opened_at, status, realized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0.0)
                """,
                (
                    nvda["occ"], nvda["ticker"], nvda["display"], nvda["tape_code"], nvda["rh_clipboard"],
                    nvda["expiry_iso"], nvda["right"], 140.0, "RH-MANUAL", 1, 1,
                    1.80, 1.98, 2.02, "SL @ 1.55", "dk_flow", "09:38:12.104",
                ),
            )

            conn.execute(
                """
                INSERT INTO positions (
                    occ_symbol, ticker, display_contract, tape_code, rh_clipboard,
                    expiry_iso, opt_right, strike, origin, qty, initial_qty,
                    avg_entry, live_bid, live_ask, stop_note, caller, opened_at, status, realized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0.0)
                """,
                (
                    tsla["occ"], tsla["ticker"], tsla["display"], tsla["tape_code"], tsla["rh_clipboard"],
                    tsla["expiry_iso"], tsla["right"], 245.0, "ALPACA-PAPER", 1, 1,
                    1.26, 1.34, 1.38, "Hold runner", "vega_desk", "09:44:05.881",
                ),
            )

            # Seed realistic Orders Blotter history
            t0 = time.time() - 420
            conn.execute(
                """
                INSERT INTO orders (
                    created_at, ts_display, signal_id, side, occ_symbol, display_contract,
                    venue, qty, caller_price, limit_price, fill_price, live_ref_price,
                    slippage_pct, status, reject_reason, expires_at_epoch, broker_order_id
                ) VALUES (?, '09:38:14.019', 1, 'BTO', ?, ?, 'RH-MANUAL', 1, 1.78, 1.80, 1.80, 1.80, 1.12, 'FILLED', '', 0, 'RH-LOCAL-001')
                """,
                (t0, nvda["occ"], nvda["display"]),
            )
            conn.execute(
                """
                INSERT INTO orders (
                    created_at, ts_display, signal_id, side, occ_symbol, display_contract,
                    venue, qty, caller_price, limit_price, fill_price, live_ref_price,
                    slippage_pct, status, reject_reason, expires_at_epoch, broker_order_id
                ) VALUES (?, '09:44:06.192', 3, 'BTO', ?, ?, 'ALPACA-PAPER', 1, 1.24, 1.30, 1.26, 1.26, 1.61, 'FILLED', '', 0, 'ALP-SIM-8841')
                """,
                (t0 + 180, tsla["occ"], tsla["display"]),
            )

            # Seed chronological signal tape including an active Co-Pilot Trade Ticket on SPY
            seed_signals = [
                (
                    "2026-10-29T09:38:11.812", "09:38:11.812", "dk_flow", "options-alerts",
                    "BTO NVDA 11/06 140C @ 1.78 starter size, stop 1.55",
                    1, "BTO", "NVDA", nvda["occ"], nvda["tape_code"], nvda["display"], nvda["rh_clipboard"],
                    nvda["expiry_iso"], "C", 140.0, 1.78, 1.77, 1.80, 1.12, "PASS",
                    0.0, "SL @ 1.55", "LOCAL_FAST", 14, "FILLED", 1, "RH-MANUAL",
                ),
                (
                    "2026-10-29T09:41:29.044", "09:41:29.044", "macro_dan", "options-alerts",
                    "watching QQQ 504.50 VWAP test before touching anything else",
                    0, "IGNORE", "", "", "", "", "", "", "", 0.0, 0.0, 0.0, 0.0, 0.0, "N/A",
                    0.0, "", "LOCAL_FAST", 9, "IGNORED", 0, "",
                ),
                (
                    "2026-10-29T09:44:05.610", "09:44:05.610", "vega_desk", "options-alerts",
                    "BTO TSLA 11/06 245P @ 1.24 breakdown play",
                    1, "BTO", "TSLA", tsla["occ"], tsla["tape_code"], tsla["display"], tsla["rh_clipboard"],
                    tsla["expiry_iso"], "P", 245.0, 1.24, 1.24, 1.26, 1.61, "PASS",
                    0.0, "", "LOCAL_FAST", 16, "AUTO_ROUTED", 1, "ALPACA-PAPER",
                ),
                (
                    "2026-10-29T09:49:52.338", "09:49:52.338", "dk_flow", "options-alerts",
                    "semis still leading tape, patience into 10am",
                    0, "IGNORE", "", "", "", "", "", "", "", 0.0, 0.0, 0.0, 0.0, 0.0, "N/A",
                    0.0, "", "LOCAL_FAST", 7, "IGNORED", 0, "",
                ),
                (
                    "2026-10-29T09:53:18.402", "09:53:18.402", "dk_flow", "options-alerts",
                    "BTO SPY 10/30 585C @ 1.45 momentum reclaim, tight risk",
                    1, "BTO", "SPY", spy["occ"], spy["tape_code"], spy["display"], spy["rh_clipboard"],
                    spy["expiry_iso"], "C", 585.0, 1.45, 1.46, 1.48, 2.07, "PASS",
                    0.0, "", "LOCAL_FAST", 18, "PENDING", 1, "RH-MANUAL",
                ),
            ]
            conn.executemany(
                """
                INSERT INTO signals (
                    ts_iso, ts_ms_display, author, channel, raw_text,
                    is_actionable, action, ticker, occ_symbol, tape_code, display_contract, rh_clipboard,
                    expiry_iso, opt_right, strike, alert_price, live_bid, live_ask, slippage_pct, slippage_verdict,
                    trim_pct, stop_note, parser_used, parse_latency_ms, ticket_status, suggested_qty, routed_origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                seed_signals,
            )
            conn.commit()

    def get_settings(self, redact_keys: bool = False) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            out = {r["key"]: r["value"] for r in rows}
        if redact_keys:
            for secret_field in (
                "gemini_api_key",
                "openai_api_key",
                "alpaca_paper_key",
                "alpaca_paper_secret",
                "alpaca_live_key",
                "alpaca_live_secret",
            ):
                val = out.get(secret_field, "")
                out[f"{secret_field}_configured"] = bool(val)
                if val:
                    out[secret_field] = f"{val[:4]}...{val[-4:]}" if len(val) > 8 else "configured"
        return out

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            for k, v in updates.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                str_val = str(v)
                # Ignore masked placeholder roundtrips
                if "..." in str_val and k.endswith(("_key", "_secret")):
                    continue
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str_val),
                )
            conn.commit()
        return self.get_settings(redact_keys=True)

    def list_signals(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            # If more than limit, grab newest limit in chronological order
            total = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
            if total > limit:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM signals ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def insert_signal(self, data: dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    ts_iso, ts_ms_display, author, channel, raw_text,
                    is_actionable, action, ticker, occ_symbol, tape_code, display_contract, rh_clipboard,
                    expiry_iso, opt_right, strike, alert_price, live_bid, live_ask, slippage_pct, slippage_verdict,
                    trim_pct, stop_note, parser_used, parse_latency_ms, ticket_status, suggested_qty, routed_origin
                ) VALUES (
                    :ts_iso, :ts_ms_display, :author, :channel, :raw_text,
                    :is_actionable, :action, :ticker, :occ_symbol, :tape_code, :display_contract, :rh_clipboard,
                    :expiry_iso, :opt_right, :strike, :alert_price, :live_bid, :live_ask, :slippage_pct, :slippage_verdict,
                    :trim_pct, :stop_note, :parser_used, :parse_latency_ms, :ticket_status, :suggested_qty, :routed_origin
                )
                """,
                data,
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_signal_status(self, signal_id: int, status: str, routed_origin: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if routed_origin is not None:
                conn.execute(
                    "UPDATE signals SET ticket_status=?, routed_origin=? WHERE id=?",
                    (status, routed_origin, signal_id),
                )
            else:
                conn.execute(
                    "UPDATE signals SET ticket_status=? WHERE id=?",
                    (status, signal_id),
                )
            conn.commit()

    def update_signal_quotes(self, signal_id: int, bid: float, ask: float, slip_pct: float, verdict: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE signals SET live_bid=?, live_ask=?, slippage_pct=?, slippage_verdict=? WHERE id=?",
                (bid, ask, slip_pct, verdict, signal_id),
            )
            conn.commit()

    def list_open_positions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_position(self, pos_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            return dict(row) if row else None

    def find_open_position_for_exit(self, ticker: str = "", occ_symbol: str = "", caller: str = "") -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            if occ_symbol:
                row = conn.execute(
                    "SELECT * FROM positions WHERE status='OPEN' AND occ_symbol=? ORDER BY id DESC LIMIT 1",
                    (occ_symbol,),
                ).fetchone()
                if row:
                    return dict(row)
            if ticker:
                row = conn.execute(
                    "SELECT * FROM positions WHERE status='OPEN' AND ticker=? ORDER BY id DESC LIMIT 1",
                    (ticker.upper(),),
                ).fetchone()
                if row:
                    return dict(row)
            if caller:
                row = conn.execute(
                    "SELECT * FROM positions WHERE status='OPEN' AND caller=? ORDER BY id DESC LIMIT 1",
                    (caller,),
                ).fetchone()
                if row:
                    return dict(row)
            row = conn.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def upsert_or_add_position(self, data: dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM positions WHERE status='OPEN' AND occ_symbol=? AND origin=?",
                (data["occ_symbol"], data["origin"]),
            ).fetchone()
            if existing:
                old_qty = int(existing["qty"])
                add_qty = int(data["qty"])
                new_qty = old_qty + add_qty
                new_avg = ((old_qty * float(existing["avg_entry"])) + (add_qty * float(data["avg_entry"]))) / max(1, new_qty)
                conn.execute(
                    """
                    UPDATE positions
                    SET qty=?, initial_qty=initial_qty+?, avg_entry=?, live_bid=?, live_ask=?, stop_note=COALESCE(NULLIF(?, ''), stop_note)
                    WHERE id=?
                    """,
                    (new_qty, add_qty, round(new_avg, 4), data["live_bid"], data["live_ask"], data.get("stop_note", ""), existing["id"]),
                )
                conn.commit()
                return int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO positions (
                    occ_symbol, ticker, display_contract, tape_code, rh_clipboard,
                    expiry_iso, opt_right, strike, origin, qty, initial_qty,
                    avg_entry, live_bid, live_ask, stop_note, caller, opened_at, status, realized_pnl
                ) VALUES (
                    :occ_symbol, :ticker, :display_contract, :tape_code, :rh_clipboard,
                    :expiry_iso, :opt_right, :strike, :origin, :qty, :initial_qty,
                    :avg_entry, :live_bid, :live_ask, :stop_note, :caller, :opened_at, 'OPEN', 0.0
                )
                """,
                data,
            )
            conn.commit()
            return int(cur.lastrowid)

    def reduce_or_close_position(self, pos_id: int, qty_to_close: int, exit_price: float) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if not row or row["status"] != "OPEN":
                return None
            cur_qty = int(row["qty"])
            close_qty = min(cur_qty, max(1, int(qty_to_close)))
            pnl_delta = (float(exit_price) - float(row["avg_entry"])) * 100.0 * close_qty
            remaining = cur_qty - close_qty
            new_status = "CLOSED" if remaining <= 0 else "OPEN"
            conn.execute(
                """
                UPDATE positions
                SET qty=?, status=?, realized_pnl=realized_pnl + ?
                WHERE id=?
                """,
                (remaining, new_status, round(pnl_delta, 2), pos_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            return dict(updated) if updated else None

    def update_position_stop(self, pos_id: int, stop_note: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE positions SET stop_note=? WHERE id=?", (stop_note, pos_id))
            conn.commit()

    def update_position_quotes(self, pos_id: int, bid: float, ask: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE positions SET live_bid=?, live_ask=? WHERE id=?", (bid, ask, pos_id))
            conn.commit()

    def insert_order(self, data: dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders (
                    created_at, ts_display, signal_id, side, occ_symbol, display_contract,
                    venue, qty, caller_price, limit_price, fill_price, live_ref_price,
                    slippage_pct, status, reject_reason, expires_at_epoch, broker_order_id
                ) VALUES (
                    :created_at, :ts_display, :signal_id, :side, :occ_symbol, :display_contract,
                    :venue, :qty, :caller_price, :limit_price, :fill_price, :live_ref_price,
                    :slippage_pct, :status, :reject_reason, :expires_at_epoch, :broker_order_id
                )
                """,
                data,
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_order(self, order_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [order_id]
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE orders SET {cols} WHERE id=?", vals)
            conn.commit()

    def compute_daily_metrics(self) -> dict[str, float]:
        """Compute Daily Risk Used (open cost basis + working buy orders) and Realized P&L."""
        with self._lock, self._connect() as conn:
            open_rows = conn.execute("SELECT qty, avg_entry FROM positions WHERE status='OPEN'").fetchall()
            open_risk = sum(int(r["qty"]) * float(r["avg_entry"]) * 100.0 for r in open_rows)

            working_rows = conn.execute(
                "SELECT qty, limit_price FROM orders WHERE status='WORKING' AND side='BTO'"
            ).fetchall()
            working_risk = sum(int(r["qty"]) * float(r["limit_price"]) * 100.0 for r in working_rows)

            realized_row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0.0) AS p FROM positions").fetchone()
            realized_pnl = float(realized_row["p"]) if realized_row else 0.0

            return {
                "daily_risk_used": round(open_risk + working_risk, 2),
                "realized_pnl": round(realized_pnl, 2),
            }

    def clear_session(self) -> None:
        """Clear all signals, orders, and positions to a blank slate while keeping settings/keys."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM orders")
            conn.commit()

    def reset_demo_state(self, seed: bool = False) -> None:
        self.clear_session()
        if seed:
            self._seed_if_empty()
