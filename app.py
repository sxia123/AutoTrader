"""AutoTrader desktop server and native app-window launcher."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend.database import StateDB
from backend.discord_listener import WindowsDiscordToastWatcher
from backend.market_and_broker import ExecutionEngine, test_alpaca_connection
from backend.parser_engine import test_parser_connection


ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"


class AutoTraderAppContext:
    def __init__(self) -> None:
        self.db = StateDB()
        self.engine = ExecutionEngine(self.db)
        self.discord_watcher = WindowsDiscordToastWatcher(
            on_message=self._on_discord_toast,
            get_channel_filter=lambda: self.db.get_settings().get("discord_channel_filter", "options-alerts"),
            is_enabled=lambda: self.db.get_settings().get("discord_listener_enabled", "true") == "true",
        )
        self._stop = threading.Event()

    def _on_discord_toast(self, body: str, author: str, channel: str) -> None:
        self.engine.ingest_discord_message(raw_text=body, author=author, channel=channel)

    def start_background_workers(self) -> None:
        self.discord_watcher.start()
        t = threading.Thread(target=self._tick_loop, name="MarketTickWorker", daemon=True)
        t.start()

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.engine.tick_market_and_orders()
            except Exception:
                pass
            time.sleep(1.0)

    def build_state_snapshot(self) -> dict[str, Any]:
        raw_settings = self.db.get_settings(redact_keys=False)
        redacted_settings = self.db.get_settings(redact_keys=True)
        has_alpaca = bool(
            (raw_settings.get("alpaca_paper_key") and raw_settings.get("alpaca_paper_secret"))
            or (raw_settings.get("alpaca_live_key") and raw_settings.get("alpaca_live_secret"))
        )
        return {
            "server_epoch": time.time(),
            "settings": redacted_settings,
            "signals": self.db.list_signals(limit=80),
            "positions": self.db.list_open_positions(),
            "orders": self.db.list_orders(limit=40),
            "metrics": self.db.compute_daily_metrics(),
            "health": {
                "discord_connected": self.discord_watcher.connected,
                "discord_status": self.discord_watcher.status_label,
                "quote_feed": "Alpaca NBBO" if has_alpaca else "Sim NBBO",
            },
        }


def make_handler(ctx: AutoTraderAppContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence noisy per-poll access logs
            return

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            body = self.rfile.read(length).decode("utf-8")
            return json.loads(body) if body else {}

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                self._send_json(ctx.build_state_snapshot())
                return

            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (FRONTEND_DIR / rel).resolve()
            if not str(target).startswith(str(FRONTEND_DIR.resolve())) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
                return

            ctype, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"Invalid JSON: {exc}"}, status=400)
                return

            if path == "/api/settings":
                updated = ctx.db.update_settings(body)
                self._send_json({"ok": True, "settings": updated})
                return

            if path == "/api/signals/inject":
                raw_text = str(body.get("raw_text") or "").strip()
                if not raw_text:
                    self._send_json({"ok": False, "error": "Empty message"}, status=400)
                    return
                author = str(body.get("author") or "dk_flow").strip()
                channel = str(
                    body.get("channel")
                    or ctx.db.get_settings().get("discord_channel_filter", "options-alerts")
                )
                sim_bias = str(body.get("sim_bias") or "normal")
                sig = ctx.engine.ingest_discord_message(
                    raw_text=raw_text,
                    author=author,
                    channel=channel,
                    sim_bias=sim_bias,
                )
                self._send_json({"ok": True, "signal": sig})
                return

            if path == "/api/copilot/decision":
                sig_id = int(body.get("signal_id") or 0)
                decision = str(body.get("decision") or "SKIP").upper()
                qty_ovr = body.get("qty_override")
                px_ovr = body.get("price_override")
                res = ctx.engine.confirm_copilot_ticket(
                    signal_id=sig_id,
                    decision="FILL" if decision == "FILL" else "SKIP",
                    qty_override=int(qty_ovr) if qty_ovr else None,
                    price_override=float(px_ovr) if px_ovr else None,
                )
                self._send_json(res)
                return

            if path == "/api/positions/action":
                pos_id = int(body.get("pos_id") or 0)
                action = str(body.get("action") or "CLOSE_ALL").upper()
                res = ctx.engine.manual_position_action(pos_id=pos_id, action=action)
                self._send_json(res)
                return

            if path == "/api/orders/cancel":
                order_id = int(body.get("order_id") or 0)
                res = ctx.engine.cancel_working_order(order_id=order_id)
                self._send_json(res)
                return

            if path == "/api/parser/test":
                stored = ctx.db.get_settings(redact_keys=False)
                merged = dict(stored)
                for k, v in body.items():
                    if isinstance(v, str) and "..." in v and k.endswith(("_key", "_secret")):
                        continue
                    if v is not None and v != "":
                        merged[k] = v
                res = test_parser_connection(merged)
                self._send_json(res)
                return

            if path == "/api/alpaca/test":
                stored = ctx.db.get_settings(redact_keys=False)
                merged = dict(stored)
                for k, v in body.items():
                    if isinstance(v, str) and "..." in v and k.endswith(("_key", "_secret")):
                        continue
                    if v is not None and v != "":
                        merged[k] = v
                res = test_alpaca_connection(merged, paper=bool(body.get("paper", True)))
                self._send_json(res)
                return

            if path == "/api/reset":
                ctx.db.reset_demo_state(seed=bool(body.get("seed", False)))
                ctx.engine._quote_cache.clear()
                ctx.engine._hydrate_initial_quotes()
                self._send_json({"ok": True})
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    return Handler


def find_free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def launch_desktop_shell(url: str) -> None:
    """Open AutoTrader in a dedicated native chromeless desktop window (--app mode) on Windows."""
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1460,900"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoTrader Options Execution Terminal")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("--no-window", action="store_true", help="Run server without opening desktop window")
    args = parser.parse_args()

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    ctx = AutoTraderAppContext()
    ctx.start_background_workers()

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ctx))
    print(f"AutoTrader Execution Terminal listening on {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_window:
        threading.Timer(0.35, launch_desktop_shell, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down AutoTrader...")
        server.server_close()


if __name__ == "__main__":
    main()

