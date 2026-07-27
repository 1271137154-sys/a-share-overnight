import logging
import time
from abc import ABC, abstractmethod
from datetime import date
import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

RENAME = {"代码":"code", "名称":"name", "最新价":"close", "涨跌幅":"pct_change", "最高":"high", "最低":"low", "今开":"open", "成交量":"volume", "成交额":"amount", "换手率":"turnover", "量比":"volume_ratio"}

class MarketDataSource(ABC):
    @abstractmethod
    def fetch_spot(self) -> pd.DataFrame: ...
    @abstractmethod
    def fetch_history(self, code: str, days: int = 35) -> pd.DataFrame: ...
    @abstractmethod
    def is_trading_day(self, trade_date: date) -> bool: ...

class AkshareDataSource(MarketDataSource):
    """Public-data adapter. Source fields can change; failures are logged and surfaced to the UI."""
    source_name = "AkShare / 东方财富"
    def __init__(self, retries=3, timeout_seconds=20):
        self.retries, self.timeout_seconds = retries, timeout_seconds
        self._spot_cache: tuple[float, pd.DataFrame] | None = None
        self._calendar_cache: tuple[float, set[str]] | None = None

    def is_trading_day(self, trade_date: date) -> bool:
        """Uses Sina's public A-share trade calendar; conservative False on failure."""
        now = time.time()
        if not self._calendar_cache or now - self._calendar_cache[0] > 86400:
            try:
                calendar = ak.tool_trade_date_hist_sina()
                column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
                self._calendar_cache = (now, set(calendar[column].astype(str).str[:10]))
            except Exception as exc:
                logger.error("Trade calendar fetch failed: %s", exc)
                return False
        return trade_date.isoformat() in self._calendar_cache[1]

    def fetch_spot(self) -> pd.DataFrame:
        if self._spot_cache and time.time() - self._spot_cache[0] < self.timeout_seconds:
            return self._spot_cache[1].copy()
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                frame = ak.stock_zh_a_spot_em().rename(columns=RENAME)
                required = set(RENAME.values()) - {"open"}
                missing = required - set(frame.columns)
                if missing: raise ValueError(f"AkShare returned missing fields: {sorted(missing)}")
                frame["code"] = frame["code"].astype(str).str.zfill(6)
                frame = frame[frame["code"].str.startswith(("60", "00")) & ~frame["name"].str.contains("ST", na=False)].copy()
                self._spot_cache = (time.time(), frame.copy())
                return frame
            except Exception as exc:
                last_error = exc
                logger.warning("AkShare request %s/%s failed: %s", attempt, self.retries, exc)
                if attempt < self.retries: time.sleep(attempt)
        raise RuntimeError("无法获取公开行情数据，请稍后重试。") from last_error

    def fetch_history(self, code: str, days: int = 35) -> pd.DataFrame:
        symbol = code[2:] if code.startswith(("sh", "sz")) else code
        try:
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq")
            return df.tail(days).rename(columns={"日期":"date", "开盘":"open", "收盘":"close", "最高":"high", "最低":"low", "成交量":"volume", "成交额":"amount", "换手率":"turnover"})
        except Exception as exc:
            logger.warning("History for %s unavailable: %s", code, exc)
            return pd.DataFrame()
