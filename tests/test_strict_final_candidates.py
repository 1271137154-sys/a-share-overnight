from scripts.run_formal_screen import choose_final_candidates


def review(**details):
    return {
        "score": 86,
        "priority_score": 86,
        "details": {
            "board_pct_change": 2.0,
            "volume_expansion_long_bear": False,
            "repeated_rally_pullbacks": False,
            **details,
        },
    }


def item(code, strategy_id, metrics, details=None):
    return {
        "code": code,
        "name": code,
        "metrics": metrics,
        "strategy_reviews": {strategy_id: review(**(details or {}))},
    }


def common_metrics(**overrides):
    result = {
        "amount": 500_000_000,
        "close_high_ratio": 0.99,
        "ma5_distance": 3.0,
        "pct_change": 5.0,
        "turnover": 8.0,
        "volume_ratio": 1.8,
        "volume_ma5_multiple": 1.5,
        "return_5d": 12.0,
    }
    result.update(overrides)
    return result


def test_final_list_keeps_strategies_independent_and_caps_at_two():
    records = [
        item("600001", "strong_close_momentum", common_metrics()),
        item("600002", "recent_limit_up_trend", common_metrics(pct_change=4.0), {"limit_up_days_ago": 3}),
        item("600003", "limit_up_reacceleration", common_metrics(pct_change=4.0), {"limit_up_days_ago": 4}),
    ]
    final, summary = choose_final_candidates(records)
    assert len(final) == 2
    assert summary["strong_close_momentum"]["strict_eligible"] == 1
    assert summary["recent_limit_up_trend"]["strict_eligible"] == 1
    assert summary["limit_up_reacceleration"]["strict_eligible"] == 1


def test_missing_or_weak_board_does_not_pass_strict_gate():
    records = [item("600001", "strong_close_momentum", common_metrics(), {"board_pct_change": None})]
    final, summary = choose_final_candidates(records)
    assert final == []
    assert summary["strong_close_momentum"]["strict_eligible"] == 0
    review_result = records[0]["strategy_reviews"]["strong_close_momentum"]
    assert review_result["strict_overnight"]["passed"] is False
    assert review_result["strict_overnight"]["failures"]
