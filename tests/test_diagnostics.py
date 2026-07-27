import pandas as pd
from app.diagnostics import metrics_for, conditions


def row(**overrides):
    values = {"code":"600001","name":"测试","close":11,"high":11,"low":10,"volume":200,"amount":1e9,"turnover":5,"volume_ratio":1.2,"pct_change":4}
    values.update(overrides)
    return pd.Series(values)


def history():
    return pd.DataFrame({"date":pd.date_range("2026-07-01", periods=15),"close":[10 + i*.05 for i in range(15)],"volume":[100]*15})


def test_prior_five_volume_excludes_screening_day():
    metrics, missing = metrics_for(row(), history(), "2026-07-27")
    assert not missing
    assert metrics["prior5_average_volume"] == 100
    assert metrics["volume_ma5_multiple"] == 2


def test_missing_turnover_is_explicitly_marked_missing():
    metrics, missing = metrics_for(row(turnover=None), history(), "2026-07-27")
    checks = conditions("limit_up_reacceleration", row(turnover=None), metrics, missing)
    assert "turnover" in missing
    assert any(check["status"] == "missing" for check in checks)
