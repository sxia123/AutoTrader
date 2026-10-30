"""Local Windows Toast Notification watcher for Discord desktop alerts (ToS-safe, no user token)."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable


class WindowsDiscordToastWatcher:
    """Monitors Windows Notification Center DB for incoming Discord desktop notifications."""

    def __init__(
        self,
        on_message: Callable[[str, str, str], None],
        get_channel_filter: Callable[[], str],
        is_enabled: Callable[[], bool],
    ) -> None:
        self._on_message = on_message
        self._get_channel_filter = get_channel_filter
        self._is_enabled = is_enabled
        self._stop_event = threading.Event()
        self._seen_order_id: int | None = None
        self.status_label: str = "Checking..."
        self.connected: bool = False
        self._wpn_path = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "Windows"
            / "Notifications"
            / "wpndatabase.db"
        )

    def start(self) -> None:
        t = threading.Thread(target=self._run_loop, name="DiscordToastWatcher", daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        if not self._wpn_path.exists():
            self.connected = False
            self.status_label = "Manual / Replay"
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="autotrader_wpn_"))
        try:
            while not self._stop_event.is_set():
                if not self._is_enabled():
                    self.connected = False
                    self.status_label = "Paused"
                    time.sleep(1.0)
                    continue

                try:
                    self._poll_once(tmpdir)
                    self.connected = True
                    self.status_label = "WinToast Active"
                except Exception:
                    self.connected = False
                    self.status_label = "Standby (Sim)"

                time.sleep(0.75)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _poll_once(self, tmpdir: Path) -> None:
        dst_db = tmpdir / "wpndatabase.db"
        shutil.copy2(self._wpn_path, dst_db)
        for suffix in ("-wal", "-shm"):
            aux = Path(str(self._wpn_path) + suffix)
            if aux.exists():
                shutil.copy2(aux, Path(str(dst_db) + suffix))

        conn = sqlite3.connect(str(dst_db))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT n.[Order] AS ord, h.PrimaryId AS app_id, n.Payload AS payload
                FROM Notification n
                JOIN NotificationHandler h ON n.HandlerId = h.RecordId
                WHERE LOWER(h.PrimaryId) LIKE '%discord%'
                   OR LOWER(CAST(n.Payload AS TEXT)) LIKE '%discord.com%'
                ORDER BY n.[Order] ASC
                """
            ).fetchall()

            if self._seen_order_id is None:
                # Baseline existing notifications on startup so old history isn't re-traded
                self._seen_order_id = max((int(r["ord"]) for r in rows), default=0)
                return

            chan_filter = self._get_channel_filter().strip().lower().lstrip("#")
            allow_all = chan_filter in ("", "*", "all", "any")
            for r in rows:
                ord_id = int(r["ord"])
                if ord_id <= self._seen_order_id:
                    continue
                self._seen_order_id = ord_id
                raw_payload = r["payload"]
                xml_str = (
                    raw_payload.decode("utf-8", errors="ignore")
                    if isinstance(raw_payload, (bytes, bytearray))
                    else str(raw_payload or "")
                )
                parsed = self._parse_toast_xml(xml_str)
                if not parsed:
                    continue
                author, channel, body = parsed
                if not allow_all and chan_filter not in channel.lower() and chan_filter not in author.lower():
                    continue
                self._on_message(body, author, channel)
        finally:
            conn.close()

    @staticmethod
    def _parse_toast_xml(xml_str: str) -> tuple[str, str, str] | None:
        if not xml_str or "<toast" not in xml_str:
            return None
        try:
            root = ET.fromstring(xml_str)
            texts = [el.text.strip() for el in root.findall(".//text") if el.text and el.text.strip()]
            # Drop trailing domain label added by Chromium web notifications
            texts = [t for t in texts if t.lower() not in ("discord.com", "ptb.discord.com", "canary.discord.com")]
            if not texts:
                return None
            if len(texts) == 1:
                return ("discord_caller", "discord", texts[0])

            header = texts[0]
            body = " ".join(texts[1:])
            # Typical Discord toast header: "Author (#🚨︱options-alerts, Server)" or "#options-alerts"
            chan_match = re.search(r"#([^,)]+)", header)
            channel = chan_match.group(1).strip() if chan_match else "dm"
            author = re.sub(r"\s*\(.*?\)\s*", "", header).strip() or "discord_caller"
            return (author, channel, body)
        except Exception:
            return None

