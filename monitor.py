#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv

from btc_monitor.alerts import build_alert_report, detect_alert
from btc_monitor.analysis import analyze_technical, assess_market, build_scenarios
from btc_monitor.config import BTC_QUANTITY, STATE_PATH, UTC, telegram_credentials
from btc_monitor.http import build_session
from btc_monitor.macro import fetch_macro_data
from btc_monitor.market import MarketDataError, fetch_market_data
from btc_monitor.news import fetch_news
from btc_monitor.report import build_daily_report
from btc_monitor.state import load_state, save_state
from btc_monitor.telegram import TelegramError, send_telegram_message

LOGGER = logging.getLogger("btc-monitor")


def _clean_sent_events(sent: dict[str, str]) -> dict[str, str]:
    cutoff = datetime.now(UTC) - timedelta(days=14)
    clean: dict[str, str] = {}
    for key, value in sent.items():
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if timestamp.astimezone(UTC) >= cutoff:
            clean[key] = value
    return clean


def _daily_state(market, technical, assessment) -> dict:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "price": round(market.price, 2),
        "btc_quantity": float(BTC_QUANTITY),
        "change_24h_pct": round(market.change_24h_pct, 4),
        "short_label": assessment.short_label,
        "medium_label": assessment.medium_label,
        "short_score": assessment.short_score,
        "medium_score": assessment.medium_score,
        "overall_score": assessment.overall_score,
        "supports": [[round(low, 2), round(high, 2)] for low, high in technical.supports],
        "resistances": [[round(low, 2), round(high, 2)] for low, high in technical.resistances],
    }


def _deliver(session, text: str, dry_run: bool) -> None:
    if dry_run:
        print(text)
        return
    token, chat_id = telegram_credentials()
    chunks = send_telegram_message(session, token, chat_id, text)
    LOGGER.info("Сообщение успешно отправлено в Telegram (%d частей)", chunks)


def run_daily(dry_run: bool) -> None:
    session = build_session()
    state = load_state()
    market = fetch_market_data(session)
    macro = fetch_macro_data(session)
    news, news_warnings = fetch_news(session, hours=24, limit=6)
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, news)
    scenarios = build_scenarios(market, technical, assessment)
    report = build_daily_report(
        market,
        technical,
        macro,
        news,
        assessment,
        scenarios,
        state.get("last_daily"),
        extra_warnings=news_warnings,
    )
    _deliver(session, report, dry_run)
    if dry_run:
        return

    now = datetime.now(UTC).isoformat()
    state["last_daily"] = _daily_state(market, technical, assessment)
    alert_state = state["alerts"]
    alert_state["last_reference_price"] = round(market.price, 2)
    alert_state["last_assessment"] = {
        "overall_score": assessment.overall_score,
        "short_label": assessment.short_label,
        "medium_label": assessment.medium_label,
    }
    sent = _clean_sent_events(alert_state.get("sent") or {})
    for item in news:
        sent[f"news:{item.fingerprint}"] = now
    alert_state["sent"] = sent
    save_state(state)
    LOGGER.info("Состояние ежедневного отчёта обновлено: %s", STATE_PATH)


def run_alerts(dry_run: bool) -> None:
    session = build_session()
    state = load_state()
    market = fetch_market_data(session)
    macro = fetch_macro_data(session)
    news, _ = fetch_news(session, hours=6, limit=8)
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, news)
    decision = detect_alert(market, technical, assessment, news, state)
    if not decision.should_send:
        LOGGER.info("Значимых новых событий нет; Telegram-сообщение не отправляется")
        return
    report = build_alert_report(decision, market, technical, assessment)
    _deliver(session, report, dry_run)
    if dry_run:
        return

    now = datetime.now(UTC).isoformat()
    alert_state = state["alerts"]
    sent = _clean_sent_events(alert_state.get("sent") or {})
    for key in decision.event_keys:
        sent[key] = now
    alert_state.update(
        {
            "last_sent_at": now,
            "last_reference_price": round(market.price, 2),
            "last_assessment": {
                "overall_score": assessment.overall_score,
                "short_label": assessment.short_label,
                "medium_label": assessment.medium_label,
            },
            "sent": sent,
        }
    )
    save_state(state)
    LOGGER.info("Состояние алертов обновлено: %s", STATE_PATH)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram BTC position monitor")
    parser.add_argument("mode", choices=("daily", "alerts"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Сформировать результат без Telegram и без изменения state",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    try:
        if args.mode == "daily":
            run_daily(args.dry_run)
        else:
            run_alerts(args.dry_run)
    except (MarketDataError, TelegramError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception as exc:  # Last-resort guard for scheduled jobs.
        LOGGER.exception("Неожиданная ошибка %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
