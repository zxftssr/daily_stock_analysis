# -*- coding: utf-8 -*-
"""
===================================
股票数据服务层
===================================

职责：
1. 封装股票数据获取逻辑
2. 提供实时行情和历史数据接口
"""

import logging
import math
from datetime import date, datetime
from typing import Optional, Dict, Any, List

from src.repositories.stock_repo import StockRepository
from src.data.stock_index_loader import get_index_stock_name
from src.services.history_loader import load_history_snapshot

logger = logging.getLogger(__name__)


class StockService:
    """
    股票数据服务
    
    封装股票数据获取的业务逻辑
    """
    
    def __init__(self):
        """初始化股票数据服务"""
        self.repo = StockRepository()
    
    def get_realtime_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票实时行情
        
        Args:
            stock_code: 股票代码
            
        Returns:
            实时行情数据字典
        """
        try:
            # 调用数据获取器获取实时行情
            from data_provider.base import DataFetcherManager
            
            manager = DataFetcherManager()
            quote = manager.get_realtime_quote(stock_code)
            
            if quote is None:
                logger.warning(f"获取 {stock_code} 实时行情失败")
                return None
            
            # UnifiedRealtimeQuote 是 dataclass，使用 getattr 安全访问字段
            # 字段映射: UnifiedRealtimeQuote -> API 响应
            # - code -> stock_code
            # - name -> stock_name
            # - price -> current_price
            # - change_amount -> change
            # - change_pct -> change_percent
            # - open_price -> open
            # - high -> high
            # - low -> low
            # - pre_close -> prev_close
            # - volume -> volume
            # - amount -> amount
            return {
                "stock_code": getattr(quote, "code", stock_code),
                "stock_name": getattr(quote, "name", None),
                "source": getattr(getattr(quote, "source", None), "value", None),
                "current_price": getattr(quote, "price", 0.0) or 0.0,
                "change": getattr(quote, "change_amount", None),
                "change_percent": getattr(quote, "change_pct", None),
                "open": getattr(quote, "open_price", None),
                "high": getattr(quote, "high", None),
                "low": getattr(quote, "low", None),
                "prev_close": getattr(quote, "pre_close", None),
                "volume": getattr(quote, "volume", None),
                "amount": getattr(quote, "amount", None),
                "update_time": datetime.now().isoformat(),
            }
            
        except ImportError:
            logger.warning("DataFetcherManager 未找到，使用占位数据")
            return self._get_placeholder_quote(stock_code)
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}", exc_info=True)
            return None
    
    def get_history_data(
        self,
        stock_code: str,
        period: str = "daily",
        days: int = 30,
        force_refresh: bool = False,
        allow_network: bool = True,
    ) -> Dict[str, Any]:
        """
        获取股票历史行情
        
        Args:
            stock_code: 股票代码
            period: K 线周期 (daily/weekly/monthly)
            days: 自然日窗口天数
            force_refresh: 是否跳过新鲜缓存并尝试刷新外部源
            allow_network: 缓存不足时是否允许访问外部行情源
            
        Returns:
            历史行情数据字典
            
        Raises:
            ValueError: 当 period 不是 daily 时抛出（weekly/monthly 暂未实现）
        """
        # 验证 period 参数，只支持 daily
        if period != "daily":
            raise ValueError(
                f"暂不支持 '{period}' 周期，目前仅支持 'daily'。"
                "weekly/monthly 聚合功能将在后续版本实现。"
            )
        
        try:
            snapshot_kwargs = {
                "stock_code": stock_code,
                "days": days,
                "force_refresh": force_refresh,
            }
            if not allow_network:
                snapshot_kwargs["allow_network"] = False
            snapshot = load_history_snapshot(**snapshot_kwargs)
            stock_name = get_index_stock_name(stock_code)
            data = self._serialize_history_rows(
                snapshot.df,
                snapshot.effective_days,
                start_date=snapshot.start_date,
                end_date=snapshot.end_date,
            )

            return {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "period": period,
                "source": snapshot.source,
                "cache_hit": snapshot.cache_hit,
                "stale": snapshot.stale,
                "partial_cache": snapshot.partial_cache,
                "as_of_date": data[-1]["date"] if data else snapshot.as_of_date,
                "actual_records": len(data),
                "requested_days": snapshot.requested_days,
                "effective_days": snapshot.effective_days,
                "message": snapshot.message,
                "persistence_error": snapshot.persistence_error,
                "data": data,
            }
            
        except ImportError:
            logger.warning("DataFetcherManager 未找到，返回空数据")
            return {"stock_code": stock_code, "period": period, "data": []}
        except Exception as e:
            logger.error(f"获取历史数据失败: {e}", exc_info=True)
            return {"stock_code": stock_code, "period": period, "data": []}

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _date_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            if value != value:  # NaN / NaT
                return None
        except Exception:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%Y-%m-%d")
            except Exception:
                return None
        text = str(value).strip()
        if not text or text.lower() in {"nat", "nan", "none"}:
            return None
        return text[:10]

    @classmethod
    def _serialize_history_rows(
        cls,
        df: Any,
        effective_days: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            date_str = cls._date_str(row.get("date"))
            open_price = cls._finite_float(row.get("open"))
            high = cls._finite_float(row.get("high"))
            low = cls._finite_float(row.get("low"))
            close = cls._finite_float(row.get("close"))

            if not date_str or open_price is None or high is None or low is None or close is None:
                continue
            if high < low or not (low <= open_price <= high) or not (low <= close <= high):
                continue

            pct_chg = cls._finite_float(row.get("pct_chg"))
            if pct_chg is None:
                pct_chg = cls._finite_float(row.get("change_percent"))

            rows.append({
                "date": date_str,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": cls._finite_float(row.get("volume")),
                "amount": cls._finite_float(row.get("amount")),
                "change_percent": pct_chg,
            })

        rows.sort(key=lambda item: item["date"])
        if start_date or end_date:
            if start_date:
                rows = [item for item in rows if item["date"] >= start_date]
            if end_date:
                rows = [item for item in rows if item["date"] <= end_date]
            return rows
        return rows[-effective_days:]
    
    def _get_placeholder_quote(self, stock_code: str) -> Dict[str, Any]:
        """
        获取占位行情数据（用于测试）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            占位行情数据
        """
        return {
            "stock_code": stock_code,
            "stock_name": f"股票{stock_code}",
            "current_price": 0.0,
            "change": None,
            "change_percent": None,
            "open": None,
            "high": None,
            "low": None,
            "prev_close": None,
            "volume": None,
            "amount": None,
            "update_time": datetime.now().isoformat(),
        }
