"""OCC option symbology utilities and date normalization."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def next_friday(ref: date | None = None) -> date:
    """Return the nearest upcoming Friday (including today if Friday)."""
    d = ref or date.today()
    days_ahead = (4 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def next_trading_day(ref: date | None = None) -> date:
    """Return the next weekday after ref."""
    d = (ref or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def normalize_expiry(raw_expiry: str | None, ref: date | None = None) -> tuple[date, bool]:
    """Normalize trader expiry shorthand ('0DTE', '11/06', '2026-11-06', 'Nov 6') to a date.

    Returns (expiry_date, was_inferred).
    """
    today = ref or date.today()
    if not raw_expiry:
        return next_friday(today), True

    token = raw_expiry.strip().lower()
    if token in ("0dte", "today"):
        return today, False
    if token in ("1dte", "tomorrow", "tmrw"):
        return next_trading_day(today), False
    if token in ("weekly", "weeklies", "wkly"):
        return next_friday(today), True

    # ISO YYYY-MM-DD
    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", token)
    if iso_match:
        y, m, d = (int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        return date(y, m, d), False

    # MM/DD or MM/DD/YY or MM/DD/YYYY
    slash_match = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", token)
    if slash_match:
        m = int(slash_match.group(1))
        d = int(slash_match.group(2))
        y_raw = slash_match.group(3)
        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            y = today.year
            candidate = date(y, m, d)
            if candidate < today - timedelta(days=2):
                y += 1
        return date(y, m, d), False

    # Month name e.g. 'nov 6' or '6 nov'
    mon_match = re.match(r"^([a-z]{3,4})\s*(\d{1,2})$", token)
    if mon_match and mon_match.group(1) in MONTH_MAP:
        m = MONTH_MAP[mon_match.group(1)]
        d = int(mon_match.group(2))
        y = today.year
        if date(y, m, d) < today - timedelta(days=2):
            y += 1
        return date(y, m, d), False

    return next_friday(today), True


def format_strike(strike: float) -> str:
    """Format strike without trailing .0 when integer."""
    if abs(strike - round(strike)) < 1e-6:
        return str(int(round(strike)))
    return f"{strike:g}"


def build_occ_symbol(ticker: str, expiry: date | str, right: str, strike: float) -> dict[str, str]:
    """Build standard compact OCC option symbol and display variants.

    Example:
      occ: 'SPY261030C00585000'
      tape_code: 'SPY 261030C585'
      display: 'SPY 10/30 585C'
      rh_clipboard: 'SPY 10/30 $585 Call'
    """
    root = re.sub(r"[^A-Z]", "", ticker.upper())
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date() if isinstance(expiry, str) else expiry
    opt_right = "C" if str(right).upper().startswith("C") else "P"
    strike_int = int(round(float(strike) * 1000))
    strike_clean = format_strike(float(strike))

    yy_mm_dd = exp_date.strftime("%y%m%d")
    mm_dd = exp_date.strftime("%m/%d")
    occ = f"{root}{yy_mm_dd}{opt_right}{strike_int:08d}"
    tape_code = f"{root} {yy_mm_dd}{opt_right}{strike_clean}"
    display = f"{root} {mm_dd} {strike_clean}{opt_right}"
    right_word = "Call" if opt_right == "C" else "Put"
    rh_clipboard = f"{root} {mm_dd} ${strike_clean} {right_word}"

    return {
        "ticker": root,
        "expiry_iso": exp_date.isoformat(),
        "right": opt_right,
        "strike_str": strike_clean,
        "occ": occ,
        "tape_code": tape_code,
        "display": display,
        "rh_clipboard": rh_clipboard,
    }

