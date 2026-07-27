import pandas as pd
import pytest
from app.strategies import analyze_universe, calculate_metrics, evaluate
from app.performance import calculate_next_day_performance
from app.database import Database

def spot(**overrides):
    data = dict(code="600001", name="测试", close=10.4, high=10.45, low=9.9, pct_change=4.5, volume=220, amount=400_000_000, turnover=8, volume_ratio=1.8)
    data.update(overrides); return pd.Series(data)

def history():
    close = [8 + i*.1 for i in range(25)]
    return pd.DataFrame({"date":pd.date_range("2026-06-01", periods=25),"close":close,"volume":[100]*25})

def test_metrics_calculates_ma_and_price_strength():
    metrics = calculate_metrics(spot(), history())
    assert metrics["ma5"] > metrics["ma10"]
    assert 0 < metrics["close_high_ratio"] <= 1
    assert metrics["ma5_distance"] is not None

def test_evaluate_reports_failed_rule():
    metrics = calculate_metrics(spot(pct_change=1), history())
    strategy = {"rules":{"pct_change_min":4,"pct_change_max":7.5,"close_above_ma5":True}}
    assert "今日涨幅" in evaluate(metrics, strategy)

def test_volume_multiple_is_correct_ratio():
    metrics = calculate_metrics(spot(volume=150), history())
    assert metrics["volume_ma5_multiple"] == 1.5

def test_next_day_returns_are_calculated_from_selection_close():
    df = pd.DataFrame({"date":["2026-07-24", "2026-07-27"], "open":[10, 10.2], "high":[10.1, 10.8], "low":[9.9, 10.0], "close":[10, 10.5]})
    measured_date, values = calculate_next_day_performance(10, df, "2026-07-24")
    assert measured_date == "2026-07-27"
    assert values["open_return"] == pytest.approx(2)
    assert values["close_return"] == pytest.approx(5)

def test_successful_task_is_idempotent(tmp_path):
    db = Database(tmp_path / "selector.db")
    assert db.begin_task("daily_screen", "2026-07-27") is True
    db.finish_task("daily_screen", "2026-07-27", "success", "saved", 2)
    assert db.begin_task("daily_screen", "2026-07-27") is False
    assert db.latest_task("daily_screen")["records_count"] == 2

def test_rejection_explains_distance_from_ma5():
    universe = pd.DataFrame([spot(close=12.0, high=12.1).to_dict()])
    strategies = [{"id":"strong_close_momentum", "rules":{"ma5_distance_max":5, "close_above_ma5":True}}]
    matches, rejected = analyze_universe(universe, {"600001": history()}, strategies)
    assert not matches
    assert "距离5日线" in rejected[0]["reason"]

def test_each_strategy_matches_independently_not_as_an_intersection():
    universe = pd.DataFrame([
        spot(code="600001", pct_change=4.5).to_dict(),
        spot(code="600002", pct_change=2.5).to_dict(),
        spot(code="600003", pct_change=6.0).to_dict(),
    ])
    strategies = [
        {"id": "strong_close_momentum", "rules": {"pct_change_min": 4, "pct_change_max": 5}},
        {"id": "recent_limit_up_trend", "rules": {"pct_change_min": 2, "pct_change_max": 3}},
        {"id": "limit_up_reacceleration", "rules": {"pct_change_min": 5.5, "pct_change_max": 6.5}},
    ]
    histories = {row["code"]: history() for _, row in universe.iterrows()}
    matches, rejected = analyze_universe(universe, histories, strategies)
    assert not rejected
    assert {item["code"]: item["strategy_ids"] for item in matches} == {
        "600001": ["strong_close_momentum"],
        "600002": ["recent_limit_up_trend"],
        "600003": ["limit_up_reacceleration"],
    }

def test_strategy_matches_are_saved_separately(tmp_path):
    db = Database(tmp_path / "selector.db")
    records = [
        {"code": "600001", "strategy_ids": ["strong_close_momentum"]},
        {"code": "600002", "strategy_ids": ["recent_limit_up_trend", "limit_up_reacceleration"]},
    ]
    db.save_strategy_matches("2026-07-27", records)
    assert db.strategy_counts("2026-07-27") == {
        "strong_close_momentum": 1, "recent_limit_up_trend": 1, "limit_up_reacceleration": 1,
    }
