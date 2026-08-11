from scripts.run_formal_screen import choose_monthly_test_pool


def item(code, strategy_id, priority):
    return {
        "code": code, "name": code, "strategy_ids": [strategy_id],
        "metrics": {"close": 10.0},
        "strategy_reviews": {strategy_id: {"score": priority, "priority_score": priority, "plus": [], "minus": []}},
    }


def test_keeps_two_per_strategy_and_one_daily_pick():
    rows = [
        item("600001", "strong_close_momentum", 80), item("600002", "strong_close_momentum", 90), item("600003", "strong_close_momentum", 70),
        item("600004", "recent_limit_up_trend", 88), item("600005", "recent_limit_up_trend", 75),
        item("600006", "limit_up_reacceleration", 87), item("600007", "limit_up_reacceleration", 65),
    ]
    result = choose_monthly_test_pool(rows)
    assert [x["code"] for x in result["strategy_top2"]["strong_close_momentum"]] == ["600002", "600001"]
    assert len(result["strategy_top2"]["recent_limit_up_trend"]) == 2
    assert result["daily_test_pick"]["code"] == "600002"


def test_never_invents_pick_when_no_strategy_matches():
    result = choose_monthly_test_pool([])
    assert result["daily_test_pick"] is None
    assert result["no_pick_reason"]
