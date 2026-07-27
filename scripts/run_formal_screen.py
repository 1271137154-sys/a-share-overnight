"""Create a date-validated formal wide-screen result.

Historical daily bars are cached locally.  On every run the current full-market
snapshot is fetched fresh and is appended/replaced as the market-date bar.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_source import AkshareDataSource
from app.database import Database
from app.diagnostics import build_report

CACHE = ROOT / "data" / "history_cache"
SITE = ROOT / "site" / "data"
GIT = shutil.which("git") or str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "git" / "cmd" / "git.exe")


def jsonable(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def read_cache(code: str):
    path = CACHE / f"{code}.json"
    if not path.exists():
        return None
    try:
        return pd.read_json(path)
    except Exception:
        return None


def write_cache(code: str, frame: pd.DataFrame):
    CACHE.mkdir(parents=True, exist_ok=True)
    frame.to_json(CACHE / f"{code}.json", orient="records", force_ascii=False)


def write_progress(success, total, failed, current, started_at, status="running", failed_details=None):
    SITE.mkdir(parents=True, exist_ok=True)
    elapsed = max((datetime.now().astimezone() - started_at).total_seconds(), 1)
    rate = success / (elapsed / 60)
    remaining = max(total - success - len(failed), 0)
    payload = {"status": status, "success": success, "total": total, "coverage": success / total if total else 0,
               "failed": len(failed), "current_code": current, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "rate_per_minute": rate, "estimated_remaining_minutes": (remaining / rate) if rate else None,
               "failed_codes": failed, "failed_details": failed_details or {}}
    (SITE / "progress.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def publish_progress():
    """Publish only the progress data; failures are non-fatal to downloading."""
    try:
        subprocess.run([GIT, "add", "site/data/progress.json"], cwd=ROOT, check=True, capture_output=True)
        changed = subprocess.run([GIT, "diff", "--cached", "--quiet"], cwd=ROOT).returncode != 0
        if changed:
            subprocess.run([GIT, "commit", "-m", "chore: update history-cache progress"], cwd=ROOT, check=True, capture_output=True)
            subprocess.run([GIT, "push", "origin", "main"], cwd=ROOT, check=True, capture_output=True)
    except Exception:
        pass


def fetch_history(source, code: str):
    cached = read_cache(code)
    if cached is not None and len(cached) >= 60:
        return cached, "cache"
    last_error = None
    for attempt in range(3):
        try:
            frame = source.fetch_history(code, days=80)
            if frame is not None and len(frame) >= 60:
                write_cache(code, frame)
                return frame, "network"
        except Exception as exc:
            last_error = exc
        time.sleep(attempt + 1)
    return None, f"failed:{last_error}" if last_error else "failed:insufficient_history"


def composite(history: pd.DataFrame, spot: pd.Series, market_date: str):
    """Use the fresh same-day snapshot as the final unadjusted daily bar."""
    frame = history.copy()
    # Tencent and Eastmoney history endpoints may report volume in different
    # units (lots versus shares).  Amount / close / volume exposes the unit:
    # around 100 means historical volume is in lots, so normalize to shares
    # before comparing it with the fresh spot snapshot.
    if {"amount", "close", "volume"}.issubset(frame.columns):
        amounts = pd.to_numeric(frame["amount"], errors="coerce")
        closes = pd.to_numeric(frame["close"], errors="coerce")
        volumes = pd.to_numeric(frame["volume"], errors="coerce")
        ratio = (amounts / (closes * volumes)).replace([float("inf"), -float("inf")], pd.NA).dropna().median()
        if ratio is not None and 50 <= float(ratio) <= 150:
            frame["volume"] = volumes * 100
    frame["date"] = frame["date"].astype(str).str[:10]
    row = {"date": market_date, "open": spot.get("open"), "high": spot.get("high"), "low": spot.get("low"), "close": spot.get("close"), "volume": spot.get("volume"), "amount": spot.get("amount"), "turnover": spot.get("turnover"), "pct_change": spot.get("pct_change")}
    frame = frame[frame["date"] != market_date]
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True).sort_values("date").tail(81)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_candidate(metrics: dict, history: pd.DataFrame, market_date: str, strategy_ids: list[str]):
    """Second-layer review score.  This never changes a strategy match.

    It only ranks stocks that already passed at least one of the three fixed
    wide-screen strategies.  Missing board-strength and intraday data stay
    explicitly pending for manual review.
    """
    score, plus, minus = 60, [], []
    amount = _as_float(metrics.get("amount")) or 0
    ma5_distance = _as_float(metrics.get("ma5_distance"))
    close_high = _as_float(metrics.get("close_high_ratio"))
    ma10_slope = _as_float(metrics.get("ma10_slope"))
    ma20_slope = _as_float(metrics.get("ma20_slope"))
    details = {
        "amount": amount, "ma5_distance": ma5_distance,
        "ma10_trend": "up" if ma10_slope is not None and ma10_slope > 0 else "not_up_or_missing",
        "ma20_trend": "up" if ma20_slope is not None and ma20_slope > 0 else "not_up_or_missing",
        "close_high_ratio": close_high,
    }
    if amount >= 500_000_000:
        score += 10; plus.append("成交额≥5亿元，流动性较好")
    elif amount >= 300_000_000:
        score += 5; plus.append("成交额≥3亿元")
    else:
        minus.append("成交额低于3亿元")
    if ma5_distance is not None:
        if 0 < ma5_distance <= 5:
            score += 8; plus.append("收盘贴近MA5（偏离≤5%）")
        elif ma5_distance <= 7:
            score += 4; plus.append("收盘仍在MA5附近")
        else:
            score -= 8; minus.append("距离MA5偏离超过7%")
    if ma10_slope is not None and ma10_slope > 0:
        score += 5; plus.append("MA10向上")
    if ma20_slope is not None and ma20_slope > 0:
        score += 3; plus.append("MA20向上")
    if close_high is not None:
        if close_high >= .985:
            score += 7; plus.append("收盘接近全天最高价")
        elif close_high >= .98:
            score += 4; plus.append("收盘强度良好")
        else:
            score -= 5; minus.append("收盘距离最高价偏远")

    frame = history.copy()
    frame["date"] = frame["date"].astype(str).str[:10]
    for field in ("open", "high", "low", "close", "volume"):
        frame[field] = pd.to_numeric(frame.get(field), errors="coerce")
    frame = frame[frame["date"] <= market_date].sort_values("date")
    limit_ups = metrics.get("limit_ups") or []
    latest_limit_date = limit_ups[-1]["date"] if limit_ups else None
    details["latest_limit_up_date"] = latest_limit_date
    details["limit_up_days_ago"] = metrics.get("limit_up_days_ago")
    max_drawdown = None
    if latest_limit_date:
        post = frame[frame["date"] >= latest_limit_date]
        start = post.iloc[0] if not post.empty else None
        start_close = _as_float(start.get("close")) if start is not None else None
        low = _as_float(post["low"].min()) if not post.empty else None
        if start_close not in (None, 0) and low is not None:
            max_drawdown = (low / start_close - 1) * 100
            if max_drawdown >= -5:
                score += 5; plus.append("涨停后最大回撤≤5%")
            elif max_drawdown >= -8:
                score += 2; plus.append("涨停后回撤可控")
            elif max_drawdown < -10:
                score -= 6; minus.append("涨停后最大回撤超过10%")
        days = metrics.get("limit_up_days_ago")
        if days is not None and 2 <= days <= 5:
            score += 5; plus.append("涨停时间窗口较合适")
        elif days is not None and 6 <= days <= 10:
            score += 3; plus.append("近期有有效涨停")
    details["max_drawdown_after_limit_up"] = max_drawdown

    post = frame[frame["date"] >= latest_limit_date] if latest_limit_date else frame.tail(10)
    long_bear = False
    reversals = 0
    if len(post) >= 2:
        vols = post["volume"].rolling(5, min_periods=1).mean().shift(1)
        previous_close = post["close"].shift(1)
        decline = post["close"] / previous_close - 1
        long_bear = bool(((post["close"] < post["open"]) & (decline <= -.03) & (post["volume"] > vols * 1.2)).fillna(False).any())
        high_range = (post["high"] - post["low"]).replace(0, pd.NA)
        upper_shadow = (post["high"] - post[["open", "close"]].max(axis=1)) / high_range
        reversals = int(((upper_shadow >= .35) & (post["close"] / post["high"] < .98)).fillna(False).sum())
    details["volume_expansion_long_bear"] = long_bear
    details["repeated_rally_pullbacks"] = reversals >= 2
    if long_bear:
        score -= 8; minus.append("出现放量长阴")
    else:
        score += 3; plus.append("未见放量长阴")
    if reversals >= 2:
        score -= 7; minus.append("连续冲高回落")
    else:
        score += 3; plus.append("未见连续冲高回落")
    if len(strategy_ids) > 1:
        score += 5; plus.append("多策略同时命中")
    score = max(0, min(100, score))
    category = "重点候选" if score >= 80 else "次级候选" if score >= 70 else "观察" if score >= 60 else "淘汰"
    return {"score": score, "category": category, "plus": plus, "minus": minus,
            "pending_manual": ["板块强度待人工确认", "尾盘分时待人工确认"], "details": details}


def main(market_date: str | None = None, use_stored_snapshot: bool = False):
    market_date = market_date or date.today().isoformat()
    now = datetime.now().astimezone()
    if now.date().isoformat() != market_date or now.hour < 15:
        raise RuntimeError(f"{market_date}行情未确认收盘，正式筛选未生成。")
    db = Database(settings.database_path)
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = db.load_snapshot(market_date) if use_stored_snapshot else source.fetch_spot()
    if spot is None or spot.empty:
        raise RuntimeError(f"{market_date}行情获取失败，今日正式筛选未生成。")
    fresh = spot[spot["code"].astype(str).str.zfill(6).str.startswith(("60", "00")) & ~spot["name"].astype(str).str.contains("ST", na=False)].copy()
    if fresh.empty:
        raise RuntimeError(f"{market_date}行情获取失败，今日正式筛选未生成。")
    fresh["code"] = fresh["code"].astype(str).str.zfill(6)
    histories, failed, failed_details, sources = {}, [], {}, {}
    started_at = datetime.now().astimezone(); total = len(fresh); workers = 4
    remaining_codes = list(fresh["code"]); start = 0
    while start < total:
        batch = remaining_codes[start:start + workers]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            jobs = {executor.submit(fetch_history, source, code): code for code in batch}
            batch_failures = 0
            for job in as_completed(jobs):
                code = jobs[job]; history, origin = job.result()
                if history is None:
                    failed.append(code); failed_details[code] = origin; batch_failures += 1
                else:
                    histories[code] = composite(history, fresh.loc[fresh["code"] == code].iloc[0], market_date)
                    sources[code] = origin
        start += len(batch); processed = start
        if processed % 100 < len(batch) or processed == total:
            write_progress(len(histories), total, failed, batch[-1], started_at, failed_details=failed_details)
            publish_progress()
        if batch_failures and workers > 1:
            workers -= 1
            time.sleep(min(30, 2 ** min(batch_failures, 5)))
    coverage = len(histories) / len(fresh)
    raw_last = max((pd.Series(frame["date"]).astype(str).str[:10].iloc[-2] for frame in histories.values() if len(frame) >= 2), default=None)
    if coverage < .95:
        raise RuntimeError(f"历史数据覆盖率 {coverage:.2%} 低于95%，今日正式筛选未生成。失败代码：{','.join(failed[:50])}")
    report = build_report(fresh, histories, market_date, source_updated_at=now.isoformat(timespec="seconds"))
    selected = []
    for code, item in report["stocks"].items():
        ids = [sid for sid, checks in item.get("strategies", {}).items() if all(c["status"] == "pass" for c in checks)]
        if ids:
            review = score_candidate(item["metrics"], histories[code], market_date, ids)
            selected.append({"code": code, "name": item["name"], "strategy_ids": ids, "metrics": item["metrics"],
                             "score": review["score"], "score_category": review["category"], "score_detail": review,
                             "failures": {sid: [c["name"] for c in checks if c["status"] != "pass"] for sid, checks in item["strategies"].items()}})
    build_id = hashlib.sha256(f"{market_date}:{now.isoformat()}:{len(selected)}".encode()).hexdigest()[:12]
    metadata = {"build_id":build_id,"data_version":"formal-wide-v1","market_date":market_date,"snapshot_date":market_date,"snapshot_source":"stored_2026-07-27_fresh_snapshot" if use_stored_snapshot else "fresh_full_market_snapshot","historical_last_date":raw_last,"used_snapshot_composite":True,"final_calculation_date":market_date,"run_at":now.isoformat(timespec="seconds"),"coverage":coverage,"history_success":len(histories),"history_failed":len(failed),"failed_codes":failed,"formal":True}
    if not use_stored_snapshot:
        db.save_snapshot(market_date, fresh, "fresh-full-market-snapshot")
    db.save_selections(market_date, selected); db.save_strategy_matches(market_date, selected)
    names = {"strong_close_momentum":"策略1：强势收盘隔夜动量（宽筛）","recent_limit_up_trend":"策略2：近期涨停后的温和趋势（宽筛）","limit_up_reacceleration":"策略3：涨停后整理再转强（宽筛）"}
    for item in selected: item["strategy_names"] = [names[key] for key in item["strategy_ids"]]
    selected.sort(key=lambda item: (-item["score"], item["code"]))
    score_counts = {
        "key_candidates": sum(item["score"] >= 80 for item in selected),
        "secondary_candidates": sum(70 <= item["score"] <= 79 for item in selected),
        "watch": sum(60 <= item["score"] <= 69 for item in selected),
        "eliminated": sum(item["score"] < 60 for item in selected),
    }
    payload = {**metadata,"records":jsonable(selected),"strategy_counts":db.strategy_counts(market_date),
               "union_count":len(selected),"multi_strategy_count":sum(len(item["strategy_ids"]) > 1 for item in selected),
               "score_counts":score_counts,"excluded_history_insufficient":failed,"rejections":[],"history_sources":sources}
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / f"{market_date}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    (SITE / "formal-latest.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    manifest_path = SITE / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("dates", []) if manifest_path.exists() else []
    dates = [market_date] + [item for item in existing if item != market_date]
    manifest_path.write_text(json.dumps({"dates": dates[:90], "latest": market_date}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(len(histories), total, failed, "", started_at, status="completed", failed_details=failed_details)
    print(json.dumps({**metadata,"counts":payload["strategy_counts"],"union":len(selected),"multi":sum(len(x["strategy_ids"])>1 for x in selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main(use_stored_snapshot="--stored-snapshot" in sys.argv)
