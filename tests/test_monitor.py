from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from btc_monitor.alerts import detect_alert
from btc_monitor.analysis import analyze_technical, assess_market, build_scenarios
from btc_monitor.config import (
    BTC_QUANTITY,
    ENTRY_PRICE_USD,
    INITIAL_POSITION_USD,
    MAURITIUS_TZ,
    UTC,
)
from btc_monitor.macro import _fomc_meeting_datetime, _fred_series, _fred_yoy_metric
from btc_monitor.models import Candle, MacroData, MacroEvent, MacroMetric, MarketData
from btc_monitor.report import build_daily_report
from btc_monitor.state import load_state, save_state
from btc_monitor.telegram import TelegramError, send_telegram_message, split_message


def market_fixture(price: float = 85_000.0) -> MarketData:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    daily: list[Candle] = []
    for index in range(260):
        close = 62_000 + index * 85 + ((index % 9) - 4) * 120
        daily.append(
            Candle(
                timestamp=start + timedelta(days=index),
                open=close - 80,
                high=close + 500,
                low=close - 500,
                close=close,
            )
        )
    daily[-1] = Candle(daily[-1].timestamp, price - 100, price + 500, price - 500, price)
    hourly = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=price * 0.999,
            high=price * 1.002,
            low=price * 0.998,
            close=price * (0.98 + 0.02 * index / 49),
        )
        for index in range(50)
    ]
    return MarketData(
        price=price,
        change_24h_pct=2.1,
        daily=daily,
        hourly=hourly,
        sources=["fixture"],
    )


def test_position_math_is_exact() -> None:
    assert BTC_QUANTITY == INITIAL_POSITION_USD / ENTRY_PRICE_USD
    assert BTC_QUANTITY.quantize(Decimal("0.00000001")) == Decimal("0.63104223")
    assert float(BTC_QUANTITY * ENTRY_PRICE_USD) == 50_000.0


def test_analysis_and_report_contain_required_sections() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    scenarios = build_scenarios(market, technical, assessment)
    report = build_daily_report(market, technical, macro, [], assessment, scenarios, previous=None)
    for expected in (
        "1. ПОЗИЦИЯ",
        "2. СТРУКТУРА РЫНКА",
        "3. МАКРО",
        "4. ВАЖНЫЕ НОВОСТИ",
        "5. ОЦЕНКА РЫНКА",
        "6. СЦЕНАРИИ",
        "ИЗМЕНЕНИЕ КАРТИНЫ СО ВЧЕРА",
        "Предыдущего отчёта пока нет",
    ):
        assert expected in report


def test_telegram_split_never_exceeds_limit() -> None:
    text = "\n\n".join(["Раздел " + ("данные " * 900) for _ in range(3)])
    chunks = split_message(text, limit=3900)
    assert len(chunks) > 3
    assert all(0 < len(chunk) <= 3900 for chunk in chunks)
    assert "Раздел" in chunks[0]


def test_alert_is_not_sent_without_significant_change() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    state = {
        "last_daily": {
            "price": market.price,
            "overall_score": assessment.overall_score,
            "short_label": assessment.short_label,
            "medium_label": assessment.medium_label,
        },
        "alerts": {
            "last_sent_at": None,
            "last_reference_price": market.price,
            "last_assessment": {"overall_score": assessment.overall_score},
            "sent": {},
        },
    }
    decision = detect_alert(market, technical, assessment, [], state)
    assert decision.should_send is False


def test_alert_detects_crossing_of_previously_stored_zone() -> None:
    market = market_fixture()
    macro = MacroData()
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    state = {
        "last_daily": {
            "price": 84_000,
            "overall_score": assessment.overall_score,
            "resistances": [[84_300, 84_500]],
            "supports": [[80_000, 80_500]],
        },
        "alerts": {
            "last_sent_at": None,
            "last_reference_price": 84_000,
            "last_assessment": {"overall_score": assessment.overall_score},
            "sent": {},
        },
    }
    decision = detect_alert(market, technical, assessment, [], state)
    assert decision.should_send is True
    assert any("сопротивления" in reason for reason in decision.reasons)


def test_telegram_connection_error_does_not_expose_token(monkeypatch) -> None:
    class FakeTelegramSession:
        def __init__(self) -> None:
            self.headers = {}

        def post(self, *args, **kwargs):
            import requests

            raise requests.ConnectionError("connection failed")

        def close(self) -> None:
            pass

    class DataSession:
        headers = {"User-Agent": "test"}

    monkeypatch.setattr("btc_monitor.telegram.requests.Session", FakeTelegramSession)
    token = "123456:SECRET_TOKEN_THAT_MUST_NOT_LEAK"
    try:
        send_telegram_message(DataSession(), token, "42", "test")
    except TelegramError as exc:
        assert token not in str(exc)
    else:
        raise AssertionError("TelegramError was expected")


def test_state_round_trip(tmp_path) -> None:
    path = tmp_path / "nested" / "state.json"
    state = load_state(path)
    state["last_daily"] = {"price": 80_123.45}
    save_state(state, path)
    assert load_state(path)["last_daily"]["price"] == 80_123.45


def test_fomc_calendar_rows_resolve_to_statement_datetimes() -> None:
    # Ordinary meeting, footnote marker, notation vote and a cross-month meeting.
    assert _fomc_meeting_datetime(2026, "September", "15-16*").day == 16
    assert _fomc_meeting_datetime(2025, "August", "22 (notation vote)").day == 22
    crossing = _fomc_meeting_datetime(2024, "Apr/May", "30-1")
    assert (crossing.year, crossing.month, crossing.day) == (2024, 5, 1)
    december = _fomc_meeting_datetime(2023, "Dec/Jan", "31-1")
    assert (december.year, december.month) == (2024, 1)
    assert _fomc_meeting_datetime(2026, "Unknown", "") is None


def test_fomc_statement_time_is_reported_in_mauritius_time() -> None:
    # 14:00 in Washington during daylight saving time is 22:00 on Mauritius.
    moment = _fomc_meeting_datetime(2026, "September", "15-16*")
    assert moment.tzinfo is MAURITIUS_TZ
    assert moment.strftime("%H:%M") == "22:00"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeFredSession:
    def __init__(self, text: str) -> None:
        self.text = text

    def get(self, *args, **kwargs) -> _FakeResponse:
        return _FakeResponse(self.text)


def _monthly_cpi_csv(column: str) -> str:
    rows = [f"{column},CPIAUCSL"]
    for month in range(15):
        year = 2025 + month // 12
        rows.append(f"{year}-{month % 12 + 1:02d}-01,{100 + month:.3f}")
    return "\n".join(rows)


def test_cpi_year_over_year_is_computed_from_monthly_index() -> None:
    session = _FakeFredSession(_monthly_cpi_csv("observation_date"))
    metric = _fred_yoy_metric(session, "CPIAUCSL", "CPI год к году")
    # Index runs 100..114, so the latest year-over-year change is 114/102 - 1.
    assert metric.value == pytest.approx((114 / 102 - 1) * 100)
    assert metric.change == pytest.approx(metric.value - (113 / 101 - 1) * 100)
    assert metric.unit == "%"
    assert metric.source == "FRED"


def test_fred_csv_parses_the_legacy_date_column_too() -> None:
    session = _FakeFredSession(_monthly_cpi_csv("DATE"))
    points = _fred_series(session, "CPIAUCSL", days=430)
    assert len(points) == 15
    assert points[-1][1] == 114.0


def test_state_keeps_defaults_when_stored_alerts_are_partial(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"version": 1, "alerts": {"last_reference_price": 80_000}}),
        encoding="utf-8",
    )
    state = load_state(path)
    assert state["alerts"]["last_reference_price"] == 80_000
    assert state["alerts"]["sent"] == {}
    assert state["alerts"]["last_sent_at"] is None
    assert state["last_daily"] is None


def test_state_survives_a_null_alerts_block(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "alerts": None}), encoding="utf-8")
    assert load_state(path)["alerts"]["sent"] == {}


def test_daily_cron_matches_eight_in_the_morning_on_mauritius() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    daily = yaml.safe_load((workflows / "daily-report.yml").read_text(encoding="utf-8"))
    # PyYAML parses the bare "on" key as the boolean True.
    triggers = daily.get("on", daily.get(True))
    minute, hour = triggers["schedule"][0]["cron"].split()[:2]
    scheduled = datetime(2026, 1, 1, int(hour), int(minute), tzinfo=UTC)
    assert scheduled.astimezone(MAURITIUS_TZ).strftime("%H:%M") == "08:00"
    assert "workflow_dispatch" in triggers


def test_every_workflow_is_valid_yaml_and_keeps_secrets_out_of_run_steps() -> None:
    workflows = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
    assert len(workflows) == 3
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        assert parsed["jobs"]
        for job in parsed["jobs"].values():
            for step in job["steps"]:
                # Secrets may only reach the process through env, never through a shell command.
                assert "secrets." not in step.get("run", "")


def test_report_includes_inflation_and_upcoming_macro_events() -> None:
    market = market_fixture()
    as_of = datetime(2026, 7, 1, tzinfo=UTC)
    macro = MacroData(
        inflation_cpi=MacroMetric("CPI год к году", 3.54, 0.12, "%", as_of, "FRED"),
        inflation_core_cpi=MacroMetric("Core CPI год к году", 2.79, None, "%", as_of, "FRED"),
        events=[
            MacroEvent(
                title="Заседание FOMC: решение по ставке ФРС",
                starts_at=datetime.now(MAURITIUS_TZ) + timedelta(days=20),
                source="Federal Reserve Board",
            )
        ],
    )
    technical = analyze_technical(market)
    assessment = assess_market(technical, macro, [])
    scenarios = build_scenarios(market, technical, assessment)
    report = build_daily_report(market, technical, macro, [], assessment, scenarios, previous=None)
    assert "CPI год к году: 3.54%" in report
    assert "Core CPI: 2.79%" in report
    assert "ускорение" in report
    assert "Заседание FOMC" in report
    assert "через 19 дн." in report or "через 20 дн." in report
