# -*- coding: utf-8 -*-
"""
===================================
股票数据相关模型
===================================

职责：
1. 定义股票实时行情模型
2. 定义历史 K 线数据模型
"""

from typing import Literal, Optional, List

from pydantic import BaseModel, Field


class StockQuote(BaseModel):
    """股票实时行情"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    current_price: float = Field(..., description="当前价格")
    change: Optional[float] = Field(None, description="涨跌额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    prev_close: Optional[float] = Field(None, description="昨收价")
    volume: Optional[float] = Field(None, description="成交量（股）")
    amount: Optional[float] = Field(None, description="成交额（元）")
    update_time: Optional[str] = Field(None, description="更新时间")
    
    class Config:
        json_schema_extra = {
            "example": {
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "current_price": 1800.00,
                "change": 15.00,
                "change_percent": 0.84,
                "open": 1785.00,
                "high": 1810.00,
                "low": 1780.00,
                "prev_close": 1785.00,
                "volume": 10000000,
                "amount": 18000000000,
                "update_time": "2024-01-01T15:00:00"
            }
        }


class KLineData(BaseModel):
    """K 线数据点"""
    
    date: str = Field(..., description="日期")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: Optional[float] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "date": "2024-01-01",
                "open": 1785.00,
                "high": 1810.00,
                "low": 1780.00,
                "close": 1800.00,
                "volume": 10000000,
                "amount": 18000000000,
                "change_percent": 0.84
            }
        }


class StockRankingItem(BaseModel):
    """股票或 ETF 榜单条目"""

    code: str = Field(..., description="标的代码")
    name: str = Field(..., description="标的名称")
    market: Literal["CN", "BSE", "HK", "US"] = Field(..., description="市场")
    industry: Optional[str] = Field(None, description="行业，缺失表示未分类")
    price: Optional[float] = Field(None, description="最新价")
    change_pct: Optional[float] = Field(None, description="涨跌幅 (%)")
    amount: Optional[float] = Field(None, description="成交额")
    volume: Optional[float] = Field(None, description="成交量")
    source: Optional[str] = Field(None, description="实际成功返回行情的数据源")
    updated_at: Optional[str] = Field(None, description="行情更新时间")
    asset_type: Literal["stock", "etf"] = Field("stock", description="标的类型")
    category: Optional[str] = Field(None, description="ETF 分类")
    benchmark_code: Optional[str] = Field(None, description="ETF 跟踪指数代码")
    benchmark_name: Optional[str] = Field(None, description="ETF 跟踪指数名称")
    drawdown_250d_pct: Optional[float] = Field(None, description="距近 250 个交易日最高收盘价回撤 (%)")
    return_20d_pct: Optional[float] = Field(None, description="近 20 个交易日收益率 (%)")
    return_60d_pct: Optional[float] = Field(None, description="近 60 个交易日收益率 (%)")
    return_250d_pct: Optional[float] = Field(None, description="近 250 个交易日收益率 (%)")
    history_as_of_date: Optional[str] = Field(None, description="ETF 历史指标截至交易日")
    history_stale: Optional[bool] = Field(None, description="ETF 历史指标是否来自旧缓存或覆盖不足")


class StockRankingsResponse(BaseModel):
    """股票行情榜单响应"""

    status: Literal["ok", "partial", "stale", "unsupported", "unavailable"] = Field(..., description="榜单状态")
    source: Optional[str] = Field(None, description="整体实际成功返回行情的数据源")
    updated_at: Optional[str] = Field(None, description="整体更新时间")
    message: Optional[str] = Field(None, description="状态说明，通常用于行情源不可用等空结果原因")
    history_as_of_date: Optional[str] = Field(None, description="ETF 历史指标共同覆盖的最早截至日期")
    history_coverage: Optional[int] = Field(None, description="具备当前历史指标的 ETF 数量")
    history_total: Optional[int] = Field(None, description="当前 ETF 历史指标候选总数")
    history_stale: Optional[bool] = Field(None, description="当前 ETF 历史指标是否存在过期或缺失")
    items: List[StockRankingItem] = Field(default_factory=list, description="榜单条目")


class EtfHistoryWarmupItem(BaseModel):
    """Single ETF warmup outcome."""

    code: str
    name: str
    status: Literal["ok", "stale", "unavailable", "error", "timeout"]
    source: Optional[str] = None
    as_of_date: Optional[str] = None
    actual_records: Optional[int] = None
    drawdown_250d_pct: Optional[float] = None
    message: Optional[str] = None


class EtfHistoryWarmupResponse(BaseModel):
    """Curated ETF pool history warmup summary."""

    status: Literal["ok", "partial", "unavailable"]
    started_at: str
    completed_at: str
    total: int
    succeeded: int
    stale: int
    failed: int
    items: List[EtfHistoryWarmupItem] = Field(default_factory=list)


class ExtractItem(BaseModel):
    """单条提取结果（代码、名称、置信度）"""

    code: Optional[str] = Field(None, description="股票代码，None 表示解析失败")
    name: Optional[str] = Field(None, description="股票名称（如有）")
    confidence: str = Field("medium", description="置信度：high/medium/low")


class ExtractFromImageResponse(BaseModel):
    """图片股票代码提取响应"""

    codes: List[str] = Field(..., description="提取的股票代码（已去重，向后兼容）")
    items: List[ExtractItem] = Field(default_factory=list, description="提取结果明细（代码+名称+置信度）")
    raw_text: Optional[str] = Field(None, description="原始 LLM 响应（调试用）")


class StockHistoryResponse(BaseModel):
    """股票历史行情响应"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    period: str = Field(..., description="K 线周期")
    source: Optional[str] = Field(None, description="数据来源，db_cache 表示本地缓存")
    cache_hit: Optional[bool] = Field(None, description="是否命中本地缓存")
    stale: Optional[bool] = Field(None, description="是否为旧缓存降级数据")
    partial_cache: Optional[bool] = Field(None, description="缓存是否未完整覆盖请求自然日窗口")
    as_of_date: Optional[str] = Field(None, description="数据截至日期")
    actual_records: Optional[int] = Field(None, description="实际返回记录数")
    requested_days: Optional[int] = Field(None, description="请求自然日窗口天数")
    effective_days: Optional[int] = Field(None, description="实际使用的自然日窗口天数")
    message: Optional[str] = Field(None, description="状态说明")
    data: List[KLineData] = Field(default_factory=list, description="K 线数据列表")
    
    class Config:
        json_schema_extra = {
            "example": {
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "period": "daily",
                "source": "db_cache",
                "cache_hit": True,
                "stale": False,
                "as_of_date": "2024-01-01",
                "actual_records": 30,
                "data": []
            }
        }
