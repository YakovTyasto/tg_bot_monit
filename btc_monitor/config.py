from __future__ import annotations

import os
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ENTRY_PRICE_USD = Decimal("79234")
INITIAL_POSITION_USD = Decimal("50000")
BTC_QUANTITY = INITIAL_POSITION_USD / ENTRY_PRICE_USD

MAURITIUS_TZ = ZoneInfo("Indian/Mauritius")
# Same object as datetime.UTC, which only exists on Python 3.11+.
UTC = timezone.utc

STATE_PATH = Path(os.getenv("STATE_PATH", "state/state.json"))
HTTP_TIMEOUT = (5, 15)
USER_AGENT = "btc-position-monitor/1.0 (+https://github.com/YakovTyasto/tg_bot_monit)"
TELEGRAM_SAFE_LIMIT = 3900


def telegram_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Не заданы обязательные переменные окружения: " + ", ".join(missing))
    return token, chat_id
