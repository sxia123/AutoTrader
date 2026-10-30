"""Live option quote engine, risk guardrails, and dual-mode execution router."""

from __future__ import annotations

import json
import math
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from backend.database import StateDB
from backend.occ import build_occ_symbol, normalize_expiry
from backend.parser_engine import parse_discord_message


class ExecutionEngine:
    """Coordinates signal ingestion, NBBO quote updates, guardrails, and mode-safe routing."""

    def __init__(self, db: StateDB) -> None:
        self.db = db
        self._quotes_lock = threading.RLock()
        # occ_symbol -> {"bid": float, "ask": float}
        self._quote_cache: dict[str, dict[str, float]] = {}
        self._hydrate_initial_quotes()

    def _hydrate_initial_quotes(self) -> None:
        for p in self.db.list_open_positions():
            self._quote_cache[p["occ_symbol"]] = {
                "bid": float(p["live_bid"]),
                "ask": float(p["live_ask"]),
            }
        for s in self.db.list_signals(limit=30):
            if s.get("occ_symbol") and float(s.get("live_ask", 0)) > 0:
                self._quote_cache.setdefault(
                    s["occ_symbol"],
                    {"bid": float(s["live_bid"]), "ask": float(s["live_ask"])},
                )

    def fetch_or_sim_quote(
        self,
        occ_symbol: str,
        ref_price: float = 0.0,
        sim_bias: str = "normal",
    ) -> tuple[float, float, str]:
        """Return (bid, ask, feed_source) using Alpaca Options Data API if configured, else micro-sim."""
        settings = self.db.get_settings(redact_keys=False)
        key = settings.get("alpaca_paper_key", "").strip() or settings.get("alpaca_live_key", "").strip()
        secret = settings.get("alpaca_paper_secret", "").strip() or settings.get("alpaca_live_secret", "").strip()

        if key and secret and occ_symbol and sim_bias == "normal":
            try:
                url = (
                    "https://data.alpaca.markets/v1beta1/options/snapshots?symbols="
                    + urllib.parse.quote(occ_symbol)
                )
                req = urllib.request.Request(
                    url,
                    headers={
                        "APCA-API-KEY-ID": key,
                        "APCA-API-SECRET-KEY": secret,
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                snap = payload.get("snapshots", {}).get(occ_symbol, {})
                q = snap.get("latestQuote", {})
                bid = float(q.get("bp", 0.0))
                ask = float(q.get("ap", 0.0))
                if bid > 0 and ask > 0:
                    with self._quotes_lock:
                        self._quote_cache[occ_symbol] = {"bid": bid, "ask": ask}
                    return round(bid, 2), round(ask, 2), "Alpaca NBBO"
            except Exception:
                pass

        with self._quotes_lock:
            existing = self._quote_cache.get(occ_symbol)
            if existing and sim_bias == "normal":
                return existing["bid"], existing["ask"], "Sim NBBO"

            base = ref_price if ref_price > 0 else 1.50
            if sim_bias == "chase":
                # Simulate option already ran +17% to +22% past caller's entry
                ask = round(base * random.uniform(1.17, 1.22), 2)
                spread = max(0.03, round(ask * 0.018, 2))
                bid = round(max(0.05, ask - spread), 2)
            elif sim_bias == "working":
                # Simulate ask slightly above max limit so order rests WORKING on book
                ask = round(base * 1.045, 2)
                bid = round(max(0.05, ask - 0.03), 2)
            else:
                # Realistic tight slippage (+0.7% to +2.6%)
                ask = round(base * random.uniform(1.007, 1.026), 2)
                spread = 0.02 if ask < 2.50 else 0.04
                bid = round(max(0.05, ask - spread), 2)

            self._quote_cache[occ_symbol] = {"bid": bid, "ask": ask}
            return bid, ask, "Sim NBBO"

    def tick_market_and_orders(self) -> None:
        """Advance simulated quotes slightly and process working order fills / auto-cancels."""
        now = time.time()
        settings = self.db.get_settings(redact_keys=False)
        max_slip = float(settings.get("max_slippage_pct", "5.0") or 5.0)

        # 1. Gentle realistic quote jitter on cached contracts
        with self._quotes_lock:
            for occ, q in list(self._quote_cache.items()):
                delta = random.choice([-0.01, 0.0, 0.0, 0.01])
                if delta != 0.0:
                    spread = max(0.02, round(q["ask"] - q["bid"], 2))
                    new_bid = max(0.05, round(q["bid"] + delta, 2))
                    new_ask = round(new_bid + spread, 2)
                    self._quote_cache[occ] = {"bid": new_bid, "ask": new_ask}

        # 2. Sync open position quotes
        for pos in self.db.list_open_positions():
            with self._quotes_lock:
                q = self._quote_cache.get(pos["occ_symbol"])
            if q:
                self.db.update_position_quotes(pos["id"], q["bid"], q["ask"])

        # 3. Sync pending Co-Pilot ticket quotes & slippage verdict
        for sig in self.db.list_signals(limit=25):
            if sig["ticket_status"] == "PENDING" and sig["occ_symbol"]:
                with self._quotes_lock:
                    q = self._quote_cache.get(sig["occ_symbol"])
                if q:
                    alert_px = float(sig["alert_price"] or 0.0)
                    if sig["action"] == "BTO" and alert_px > 0:
                        slip = round(((q["ask"] - alert_px) / alert_px) * 100.0, 2)
                        verdict = "PASS" if slip <= max_slip else "REJECT"
                    else:
                        slip = 0.0
                        verdict = "PASS"
                    self.db.update_signal_quotes(sig["id"], q["bid"], q["ask"], slip, verdict)

        # 4. Evaluate WORKING orders (fill if marketable after brief book dwell, or auto-cancel on expiry)
        for order in self.db.list_orders(limit=30):
            if order["status"] != "WORKING":
                continue

            exp_epoch = float(order["expires_at_epoch"] or 0.0)
            if exp_epoch > 0 and now >= exp_epoch:
                self._cancel_broker_order_if_needed(order, settings)
                self.db.update_order(
                    order["id"],
                    status="CANCELED",
                    reject_reason="Auto-cancel timer expired (unfilled)",
                )
                continue

            with self._quotes_lock:
                q = self._quote_cache.get(order["occ_symbol"])
            if not q:
                continue

            age = now - float(order["created_at"])
            # Keep WORKING visible for at least 3.5s so operator can inspect or cancel
            if age < 3.5:
                self.db.update_order(
                    order["id"],
                    live_ref_price=q["ask"] if order["side"] == "BTO" else q["bid"],
                )
                continue

            if order["side"] == "BTO" and q["ask"] <= float(order["limit_price"]):
                fill_px = q["ask"]
                caller_px = float(order["caller_price"] or fill_px)
                slip_pct = round(((fill_px - caller_px) / caller_px) * 100.0, 2) if caller_px > 0 else 0.0
                self.db.update_order(
                    order["id"],
                    status="FILLED",
                    fill_price=fill_px,
                    live_ref_price=fill_px,
                    slippage_pct=slip_pct,
                )
                sig = self.db.get_signal(int(order["signal_id"])) if order["signal_id"] else None
                if sig:
                    self.db.update_signal_status(sig["id"], "FILLED", order["venue"])
                    self.db.upsert_or_add_position(
                        {
                            "occ_symbol": sig["occ_symbol"],
                            "ticker": sig["ticker"],
                            "display_contract": sig["display_contract"],
                            "tape_code": sig["tape_code"],
                            "rh_clipboard": sig["rh_clipboard"],
                            "expiry_iso": sig["expiry_iso"],
                            "opt_right": sig["opt_right"],
                            "strike": float(sig["strike"]),
                            "origin": order["venue"],
                            "qty": int(order["qty"]),
                            "initial_qty": int(order["qty"]),
                            "avg_entry": fill_px,
                            "live_bid": q["bid"],
                            "live_ask": q["ask"],
                            "stop_note": sig.get("stop_note", ""),
                            "caller": sig.get("author", ""),
                            "opened_at": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                        }
                    )
            else:
                self.db.update_order(
                    order["id"],
                    live_ref_price=q["ask"] if order["side"] == "BTO" else q["bid"],
                )

    def ingest_discord_message(
        self,
        raw_text: str,
        author: str = "dk_flow",
        channel: str = "options-alerts",
        sim_bias: str = "normal",
    ) -> dict[str, Any]:
        """Parse, price, guardrail-check, and route a Discord message."""
        now = datetime.now()
        ts_iso = now.isoformat(timespec="milliseconds")
        ts_ms = now.strftime("%H:%M:%S.%f")[:-3]

        settings = self.db.get_settings(redact_keys=False)
        open_positions = self.db.list_open_positions()

        parsed, parser_used, latency_ms = parse_discord_message(
            raw_text=raw_text,
            author=author,
            open_positions=open_positions,
            settings=settings,
        )
        self.db.update_settings({"last_parse_latency_ms": str(latency_ms)})

        action = str(parsed.get("action", "IGNORE")).upper()
        is_actionable = bool(parsed.get("is_actionable")) and action != "IGNORE"

        if not is_actionable:
            sig_data = {
                "ts_iso": ts_iso,
                "ts_ms_display": ts_ms,
                "author": author,
                "channel": channel,
                "raw_text": raw_text,
                "is_actionable": 0,
                "action": "IGNORE",
                "ticker": "",
                "occ_symbol": "",
                "tape_code": "",
                "display_contract": "",
                "rh_clipboard": "",
                "expiry_iso": "",
                "opt_right": "",
                "strike": 0.0,
                "alert_price": 0.0,
                "live_bid": 0.0,
                "live_ask": 0.0,
                "slippage_pct": 0.0,
                "slippage_verdict": "N/A",
                "trim_pct": 0.0,
                "stop_note": "",
                "parser_used": parser_used,
                "parse_latency_ms": latency_ms,
                "ticket_status": "IGNORED",
                "suggested_qty": 0,
                "routed_origin": "",
            }
            sig_id = self.db.insert_signal(sig_data)
            return self.db.get_signal(sig_id) or sig_data

        # Handle exits / trims / stop updates using Origin-Locked Routing
        if action in ("TRIM", "STC", "STOP_UPDATE"):
            return self._route_followup_signal(
                parsed=parsed,
                raw_text=raw_text,
                author=author,
                channel=channel,
                ts_iso=ts_iso,
                ts_ms=ts_ms,
                parser_used=parser_used,
                latency_ms=latency_ms,
                settings=settings,
            )

        # Handle new BTO entry
        ticker = str(parsed.get("ticker", "")).upper()
        strike = float(parsed.get("strike") or 0.0)
        right = str(parsed.get("right", "C")).upper() or "C"
        exp_date, _ = normalize_expiry(str(parsed.get("expiry") or "") or None)
        sym = build_occ_symbol(ticker, exp_date, right, strike)

        alert_price = float(parsed.get("alert_price") or 0.0)
        bid, ask, _ = self.fetch_or_sim_quote(sym["occ"], ref_price=alert_price or 1.50, sim_bias=sim_bias)
        if alert_price <= 0:
            alert_price = ask

        max_slip = float(settings.get("max_slippage_pct", "5.0") or 5.0)
        slip_pct = round(((ask - alert_price) / alert_price) * 100.0, 2) if alert_price > 0 else 0.0
        verdict = "PASS" if slip_pct <= max_slip else "REJECT"

        # Suggested position sizing from risk rules
        metrics = self.db.compute_daily_metrics()
        daily_cap = float(settings.get("daily_risk_cap", "500.0") or 500.0)
        per_trade_cap = float(settings.get("max_risk_per_trade", "250.0") or 250.0)
        remaining_budget = max(0.0, daily_cap - metrics["daily_risk_used"])
        usable_budget = min(per_trade_cap, remaining_budget)
        contract_cost = max(1.0, ask * 100.0)
        suggested_qty = max(1, int(math.floor(usable_budget / contract_cost))) if usable_budget >= contract_cost else 1
        exceeds_daily_risk = remaining_budget < contract_cost

        mode = settings.get("execution_mode", "COPILOT")
        paused = settings.get("pause_entries", "false").lower() == "true"
        venue = self._mode_to_venue(mode)

        ticket_status = "PENDING" if mode == "COPILOT" else "AUTO_ROUTED"
        if paused:
            ticket_status = "PAUSED"

        sig_data = {
            "ts_iso": ts_iso,
            "ts_ms_display": ts_ms,
            "author": author,
            "channel": channel,
            "raw_text": raw_text,
            "is_actionable": 1,
            "action": "BTO",
            "ticker": sym["ticker"],
            "occ_symbol": sym["occ"],
            "tape_code": sym["tape_code"],
            "display_contract": sym["display"],
            "rh_clipboard": sym["rh_clipboard"],
            "expiry_iso": sym["expiry_iso"],
            "opt_right": sym["right"],
            "strike": strike,
            "alert_price": alert_price,
            "live_bid": bid,
            "live_ask": ask,
            "slippage_pct": slip_pct,
            "slippage_verdict": verdict,
            "trim_pct": 0.0,
            "stop_note": str(parsed.get("stop_note") or ""),
            "parser_used": parser_used,
            "parse_latency_ms": latency_ms,
            "ticket_status": ticket_status,
            "suggested_qty": suggested_qty,
            "routed_origin": venue,
        }
        sig_id = self.db.insert_signal(sig_data)

        # If in Paper Auto or Live Auto and not paused, route immediately through Order Engine
        if mode in ("PAPER_AUTO", "LIVE_AUTO") and not paused:
            self._auto_submit_entry_order(
                sig_id=sig_id,
                sig_data=sig_data,
                venue=venue,
                exceeds_daily_risk=exceeds_daily_risk,
                settings=settings,
            )

        return self.db.get_signal(sig_id) or sig_data

    def _route_followup_signal(
        self,
        parsed: dict[str, Any],
        raw_text: str,
        author: str,
        channel: str,
        ts_iso: str,
        ts_ms: str,
        parser_used: str,
        latency_ms: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        action = str(parsed.get("action", "STC")).upper()
        ticker = str(parsed.get("ticker", "")).upper()
        pos = self.db.find_open_position_for_exit(ticker=ticker, caller=author)

        if not pos:
            # No matching open position -> record signal as unmatched/skipped
            sig_data = {
                "ts_iso": ts_iso,
                "ts_ms_display": ts_ms,
                "author": author,
                "channel": channel,
                "raw_text": raw_text,
                "is_actionable": 1,
                "action": action,
                "ticker": ticker or "UNMATCHED",
                "occ_symbol": "",
                "tape_code": f"{ticker or 'POS'} (NO OPEN POS)",
                "display_contract": f"{ticker or 'Position'} (None Open)",
                "rh_clipboard": "",
                "expiry_iso": "",
                "opt_right": "",
                "strike": 0.0,
                "alert_price": float(parsed.get("alert_price") or 0.0),
                "live_bid": 0.0,
                "live_ask": 0.0,
                "slippage_pct": 0.0,
                "slippage_verdict": "N/A",
                "trim_pct": float(parsed.get("trim_pct") or 100.0),
                "stop_note": str(parsed.get("stop_note") or ""),
                "parser_used": parser_used,
                "parse_latency_ms": latency_ms,
                "ticket_status": "SKIPPED",
                "suggested_qty": 0,
                "routed_origin": "NONE",
            }
            sig_id = self.db.insert_signal(sig_data)
            return self.db.get_signal(sig_id) or sig_data

        origin = pos["origin"]  # CRITICAL: Lock exit routing to where position was opened!
        bid, ask, _ = self.fetch_or_sim_quote(pos["occ_symbol"], ref_price=float(pos["live_bid"]))
        trim_pct = float(parsed.get("trim_pct") or (50.0 if action == "TRIM" else 100.0))
        close_qty = int(pos["qty"]) if action == "STC" else max(1, int(round(int(pos["qty"]) * (trim_pct / 100.0))))
        stop_note = str(parsed.get("stop_note") or "")

        if action == "STOP_UPDATE":
            self.db.update_position_stop(pos["id"], stop_note or "SL @ BE")
            ticket_status = "FILLED"
        elif origin == "RH-MANUAL":
            # Manual Robinhood position -> ALWAYS surface Co-Pilot Exit Ticket so operator sells on RH
            ticket_status = "PENDING"
        else:
            # ALPACA-PAPER or ALPACA-LIVE -> Auto-execute exit on that exact venue regardless of current UI mode
            ticket_status = "AUTO_ROUTED"

        sig_data = {
            "ts_iso": ts_iso,
            "ts_ms_display": ts_ms,
            "author": author,
            "channel": channel,
            "raw_text": raw_text,
            "is_actionable": 1,
            "action": action,
            "ticker": pos["ticker"],
            "occ_symbol": pos["occ_symbol"],
            "tape_code": pos["tape_code"],
            "display_contract": pos["display_contract"],
            "rh_clipboard": pos["rh_clipboard"],
            "expiry_iso": pos["expiry_iso"],
            "opt_right": pos["opt_right"],
            "strike": float(pos["strike"]),
            "alert_price": float(parsed.get("alert_price") or bid),
            "live_bid": bid,
            "live_ask": ask,
            "slippage_pct": 0.0,
            "slippage_verdict": "PASS",
            "trim_pct": trim_pct,
            "stop_note": stop_note,
            "parser_used": parser_used,
            "parse_latency_ms": latency_ms,
            "ticket_status": ticket_status,
            "suggested_qty": close_qty,
            "routed_origin": origin,
        }
        sig_id = self.db.insert_signal(sig_data)

        if action in ("TRIM", "STC") and origin in ("ALPACA-PAPER", "ALPACA-LIVE"):
            broker_id = self._submit_alpaca_order_if_configured(
                venue=origin,
                occ_symbol=pos["occ_symbol"],
                side="sell",
                qty=close_qty,
                limit_price=bid,
                settings=settings,
            )
            self.db.reduce_or_close_position(pos["id"], close_qty, bid)
            self.db.insert_order(
                {
                    "created_at": time.time(),
                    "ts_display": ts_ms,
                    "signal_id": sig_id,
                    "side": "STC",
                    "occ_symbol": pos["occ_symbol"],
                    "display_contract": pos["display_contract"],
                    "venue": origin,
                    "qty": close_qty,
                    "caller_price": float(sig_data["alert_price"]),
                    "limit_price": bid,
                    "fill_price": bid,
                    "live_ref_price": bid,
                    "slippage_pct": 0.0,
                    "status": "FILLED",
                    "reject_reason": "",
                    "expires_at_epoch": 0.0,
                    "broker_order_id": broker_id or f"{origin[:3]}-EXIT-{sig_id}",
                }
            )
            self.db.update_signal_status(sig_id, "FILLED", origin)

        return self.db.get_signal(sig_id) or sig_data

    def _auto_submit_entry_order(
        self,
        sig_id: int,
        sig_data: dict[str, Any],
        venue: str,
        exceeds_daily_risk: bool,
        settings: dict[str, Any],
    ) -> None:
        now = time.time()
        ts_ms = sig_data["ts_ms_display"]
        max_slip = float(settings.get("max_slippage_pct", "5.0") or 5.0)
        cancel_sec = int(float(settings.get("auto_cancel_seconds", "15") or 15))
        alert_px = float(sig_data["alert_price"])
        ask = float(sig_data["live_ask"])
        slip_pct = float(sig_data["slippage_pct"])
        qty = int(sig_data["suggested_qty"] or 1)
        max_limit = round(alert_px * (1.0 + max_slip / 100.0), 2)

        if sig_data["slippage_verdict"] == "REJECT":
            self.db.insert_order(
                {
                    "created_at": now,
                    "ts_display": ts_ms,
                    "signal_id": sig_id,
                    "side": "BTO",
                    "occ_symbol": sig_data["occ_symbol"],
                    "display_contract": sig_data["display_contract"],
                    "venue": venue,
                    "qty": qty,
                    "caller_price": alert_px,
                    "limit_price": max_limit,
                    "fill_price": 0.0,
                    "live_ref_price": ask,
                    "slippage_pct": slip_pct,
                    "status": "REJECTED",
                    "reject_reason": f"Slippage +{slip_pct:.1f}% > {max_slip:.1f}% max",
                    "expires_at_epoch": 0.0,
                    "broker_order_id": "",
                }
            )
            self.db.update_signal_status(sig_id, "SKIPPED", venue)
            return

        if exceeds_daily_risk:
            self.db.insert_order(
                {
                    "created_at": now,
                    "ts_display": ts_ms,
                    "signal_id": sig_id,
                    "side": "BTO",
                    "occ_symbol": sig_data["occ_symbol"],
                    "display_contract": sig_data["display_contract"],
                    "venue": venue,
                    "qty": qty,
                    "caller_price": alert_px,
                    "limit_price": max_limit,
                    "fill_price": 0.0,
                    "live_ref_price": ask,
                    "slippage_pct": slip_pct,
                    "status": "REJECTED",
                    "reject_reason": "Exceeds Daily Risk Cap",
                    "expires_at_epoch": 0.0,
                    "broker_order_id": "",
                }
            )
            self.db.update_signal_status(sig_id, "SKIPPED", venue)
            return

        submit_limit = round(min(max_limit, ask), 2)
        broker_id, broker_err = self._submit_alpaca_order_with_status(
            venue=venue,
            occ_symbol=sig_data["occ_symbol"],
            side="buy",
            qty=qty,
            limit_price=submit_limit,
            settings=settings,
        )

        if broker_err:
            self.db.insert_order(
                {
                    "created_at": now,
                    "ts_display": ts_ms,
                    "signal_id": sig_id,
                    "side": "BTO",
                    "occ_symbol": sig_data["occ_symbol"],
                    "display_contract": sig_data["display_contract"],
                    "venue": venue,
                    "qty": qty,
                    "caller_price": alert_px,
                    "limit_price": submit_limit,
                    "fill_price": 0.0,
                    "live_ref_price": ask,
                    "slippage_pct": slip_pct,
                    "status": "REJECTED",
                    "reject_reason": f"Alpaca: {broker_err}",
                    "expires_at_epoch": 0.0,
                    "broker_order_id": "",
                }
            )
            self.db.update_signal_status(sig_id, "SKIPPED", venue)
            return

        self.db.insert_order(
            {
                "created_at": now,
                "ts_display": ts_ms,
                "signal_id": sig_id,
                "side": "BTO",
                "occ_symbol": sig_data["occ_symbol"],
                "display_contract": sig_data["display_contract"],
                "venue": venue,
                "qty": qty,
                "caller_price": alert_px,
                "limit_price": submit_limit,
                "fill_price": 0.0,
                "live_ref_price": ask,
                "slippage_pct": slip_pct,
                "status": "WORKING",
                "reject_reason": "",
                "expires_at_epoch": now + cancel_sec,
                "broker_order_id": broker_id or f"{venue[:3]}-WRK-{sig_id}",
            }
        )

    def confirm_copilot_ticket(
        self,
        signal_id: int,
        decision: str,
        qty_override: int | None = None,
        price_override: float | None = None,
    ) -> dict[str, Any]:
        """Handle Co-Pilot [Enter] Mark Filled or [Esc] Skip actions."""
        sig = self.db.get_signal(signal_id)
        if not sig:
            return {"ok": False, "error": "Signal not found"}

        if decision == "SKIP":
            self.db.update_signal_status(signal_id, "SKIPPED", "RH-MANUAL")
            return {"ok": True, "status": "SKIPPED"}

        qty = max(1, int(qty_override or sig["suggested_qty"] or 1))
        now_ms = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if sig["action"] == "BTO":
            fill_px = float(price_override or sig["live_ask"] or sig["alert_price"])
            caller_px = float(sig["alert_price"] or fill_px)
            slip_pct = round(((fill_px - caller_px) / caller_px) * 100.0, 2) if caller_px > 0 else 0.0

            self.db.upsert_or_add_position(
                {
                    "occ_symbol": sig["occ_symbol"],
                    "ticker": sig["ticker"],
                    "display_contract": sig["display_contract"],
                    "tape_code": sig["tape_code"],
                    "rh_clipboard": sig["rh_clipboard"],
                    "expiry_iso": sig["expiry_iso"],
                    "opt_right": sig["opt_right"],
                    "strike": float(sig["strike"]),
                    "origin": "RH-MANUAL",
                    "qty": qty,
                    "initial_qty": qty,
                    "avg_entry": fill_px,
                    "live_bid": float(sig["live_bid"]),
                    "live_ask": float(sig["live_ask"]),
                    "stop_note": sig.get("stop_note", ""),
                    "caller": sig.get("author", ""),
                    "opened_at": now_ms,
                }
            )
            self.db.insert_order(
                {
                    "created_at": time.time(),
                    "ts_display": now_ms,
                    "signal_id": signal_id,
                    "side": "BTO",
                    "occ_symbol": sig["occ_symbol"],
                    "display_contract": sig["display_contract"],
                    "venue": "RH-MANUAL",
                    "qty": qty,
                    "caller_price": caller_px,
                    "limit_price": fill_px,
                    "fill_price": fill_px,
                    "live_ref_price": fill_px,
                    "slippage_pct": slip_pct,
                    "status": "FILLED",
                    "reject_reason": "",
                    "expires_at_epoch": 0.0,
                    "broker_order_id": f"RH-MANUAL-{signal_id}",
                }
            )
            self.db.update_signal_status(signal_id, "FILLED", "RH-MANUAL")
            return {"ok": True, "status": "FILLED"}

        if sig["action"] in ("TRIM", "STC"):
            pos = self.db.find_open_position_for_exit(occ_symbol=sig["occ_symbol"], ticker=sig["ticker"])
            exit_px = float(price_override or sig["live_bid"] or sig["alert_price"])
            if pos:
                self.db.reduce_or_close_position(pos["id"], qty, exit_px)
                self.db.insert_order(
                    {
                        "created_at": time.time(),
                        "ts_display": now_ms,
                        "signal_id": signal_id,
                        "side": "STC",
                        "occ_symbol": pos["occ_symbol"],
                        "display_contract": pos["display_contract"],
                        "venue": pos["origin"],
                        "qty": qty,
                        "caller_price": float(sig["alert_price"] or exit_px),
                        "limit_price": exit_px,
                        "fill_price": exit_px,
                        "live_ref_price": exit_px,
                        "slippage_pct": 0.0,
                        "status": "FILLED",
                        "reject_reason": "",
                        "expires_at_epoch": 0.0,
                        "broker_order_id": f"RH-EXIT-{signal_id}",
                    }
                )
            self.db.update_signal_status(signal_id, "FILLED", "RH-MANUAL")
            return {"ok": True, "status": "FILLED"}

        self.db.update_signal_status(signal_id, "FILLED", "RH-MANUAL")
        return {"ok": True, "status": "FILLED"}

    def manual_position_action(self, pos_id: int, action: str) -> dict[str, Any]:
        """Execute inline row actions ('TRIM_50' or 'CLOSE_ALL') respecting position origin."""
        pos = self.db.get_position(pos_id)
        if not pos or pos["status"] != "OPEN":
            return {"ok": False, "error": "Position not open"}

        cur_qty = int(pos["qty"])
        close_qty = max(1, cur_qty // 2) if action == "TRIM_50" and cur_qty > 1 else (1 if action == "TRIM_50" else cur_qty)
        if action == "CLOSE_ALL":
            close_qty = cur_qty

        exit_px = float(pos["live_bid"])
        settings = self.db.get_settings(redact_keys=False)
        origin = pos["origin"]

        broker_id = ""
        if origin in ("ALPACA-PAPER", "ALPACA-LIVE"):
            broker_id = self._submit_alpaca_order_if_configured(
                venue=origin,
                occ_symbol=pos["occ_symbol"],
                side="sell",
                qty=close_qty,
                limit_price=exit_px,
                settings=settings,
            )

        self.db.reduce_or_close_position(pos_id, close_qty, exit_px)
        now_ms = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.db.insert_order(
            {
                "created_at": time.time(),
                "ts_display": now_ms,
                "signal_id": 0,
                "side": "STC",
                "occ_symbol": pos["occ_symbol"],
                "display_contract": pos["display_contract"],
                "venue": origin,
                "qty": close_qty,
                "caller_price": exit_px,
                "limit_price": exit_px,
                "fill_price": exit_px,
                "live_ref_price": exit_px,
                "slippage_pct": 0.0,
                "status": "FILLED",
                "reject_reason": "",
                "expires_at_epoch": 0.0,
                "broker_order_id": broker_id or f"{origin[:3]}-MAN-{int(time.time()) % 10000}",
            }
        )
        return {"ok": True}

    def cancel_working_order(self, order_id: int) -> dict[str, Any]:
        settings = self.db.get_settings(redact_keys=False)
        for o in self.db.list_orders(limit=50):
            if o["id"] == order_id and o["status"] == "WORKING":
                self._cancel_broker_order_if_needed(o, settings)
                self.db.update_order(order_id, status="CANCELED", reject_reason="Canceled by operator")
                return {"ok": True}
        return {"ok": False, "error": "Working order not found"}

    @staticmethod
    def _mode_to_venue(mode: str) -> str:
        if mode == "PAPER_AUTO":
            return "ALPACA-PAPER"
        if mode == "LIVE_AUTO":
            return "ALPACA-LIVE"
        return "RH-MANUAL"

    def _submit_alpaca_order_if_configured(
        self,
        venue: str,
        occ_symbol: str,
        side: str,
        qty: int,
        limit_price: float,
        settings: dict[str, Any],
    ) -> str:
        order_id, _ = self._submit_alpaca_order_with_status(
            venue=venue,
            occ_symbol=occ_symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            settings=settings,
        )
        return order_id

    def _submit_alpaca_order_with_status(
        self,
        venue: str,
        occ_symbol: str,
        side: str,
        qty: int,
        limit_price: float,
        settings: dict[str, Any],
    ) -> tuple[str, str]:
        """Submit real option limit order to Alpaca Paper or Live API when credentials exist.

        Returns (broker_order_id, error_message). Both empty when credentials are not configured.
        """
        if venue == "ALPACA-PAPER":
            key = settings.get("alpaca_paper_key", "").strip()
            secret = settings.get("alpaca_paper_secret", "").strip()
            base_url = "https://paper-api.alpaca.markets"
        elif venue == "ALPACA-LIVE":
            key = settings.get("alpaca_live_key", "").strip()
            secret = settings.get("alpaca_live_secret", "").strip()
            base_url = "https://api.alpaca.markets"
        else:
            return "", ""

        if not (key and secret):
            return "", ""

        payload = {
            "symbol": occ_symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
        }
        try:
            req = urllib.request.Request(
                f"{base_url}/v2/orders",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                out = json.loads(resp.read().decode("utf-8"))
                return str(out.get("id", "")), ""
        except urllib.error.HTTPError as http_err:
            try:
                err_body = json.loads(http_err.read().decode("utf-8", errors="ignore"))
                msg = str(err_body.get("message") or f"HTTP {http_err.code}")
            except Exception:
                msg = f"HTTP {http_err.code}"
            return "", msg
        except Exception as exc:
            return "", str(exc)

    def _cancel_broker_order_if_needed(self, order: dict[str, Any], settings: dict[str, Any]) -> None:
        broker_id = str(order.get("broker_order_id") or "")
        venue = str(order.get("venue") or "")
        if not broker_id or broker_id.startswith(("ALP-", "RH-", "LIVE-")):
            return
        if venue == "ALPACA-PAPER":
            key = settings.get("alpaca_paper_key", "").strip()
            secret = settings.get("alpaca_paper_secret", "").strip()
            base_url = "https://paper-api.alpaca.markets"
        elif venue == "ALPACA-LIVE":
            key = settings.get("alpaca_live_key", "").strip()
            secret = settings.get("alpaca_live_secret", "").strip()
            base_url = "https://api.alpaca.markets"
        else:
            return
        if not (key and secret):
            return
        try:
            req = urllib.request.Request(
                f"{base_url}/v2/orders/{urllib.parse.quote(broker_id)}",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=3.0).close()
        except Exception:
            pass


def test_alpaca_connection(settings: dict[str, Any], paper: bool = True) -> dict[str, Any]:
    """Verify Alpaca Paper/Live credentials against GET /v2/account."""
    key = settings.get("alpaca_paper_key" if paper else "alpaca_live_key", "").strip()
    secret = settings.get("alpaca_paper_secret" if paper else "alpaca_live_secret", "").strip()
    base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"

    if not key or not secret:
        return {"ok": False, "error": "Enter both Alpaca API Key and Secret first."}

    hint = ""
    if len(secret) < 40:
        hint = f" (Note: Secret length is {len(secret)} chars; standard Alpaca secrets are 40 chars — check if the right edge was clipped)"

    try:
        req = urllib.request.Request(
            f"{base_url}/v2/account",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            acct = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "status": acct.get("status", "ACTIVE"),
            "buying_power": acct.get("buying_power", "0"),
            "equity": acct.get("equity", "0"),
            "options_approved_level": acct.get("options_approved_level", acct.get("options_trading_level", "N/A")),
        }
    except urllib.error.HTTPError as http_err:
        return {"ok": False, "error": f"HTTP {http_err.code} Unauthorized/Forbidden{hint}"}
    except Exception as exc:
        return {"ok": False, "error": f"{exc}{hint}"}


