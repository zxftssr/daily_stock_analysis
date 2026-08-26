import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  BarChart3,
  ChartCandlestick,
  LineChart,
  MessageSquareQuote,
  Play,
  RefreshCw,
  Search,
  Star,
  Target,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { analysisApi, DuplicateTaskError } from '../api/analysis';
import {
  stocksApi,
  type RankingDirection,
  type RankingMetric,
  type RankingStatus,
  type StockRankingItem,
} from '../api/stocks';
import { AppPage, Badge, Button, EmptyState, InlineAlert, Input, Pagination, Select, Tooltip } from '../components/common';
import { StockKLineDrawer } from '../components/stocks/StockKLineDrawer';
import { WatchlistStarButton } from '../components/stocks/WatchlistStarButton';
import { useWatchlistConfig } from '../hooks/useWatchlistConfig';
import { useStockIndex } from '../hooks/useStockIndex';
import type { Market } from '../types/stockIndex';
import { searchStocks } from '../utils/searchStocks';
import { cn } from '../utils/cn';

const UNCATEGORIZED_INDUSTRY = '__uncategorized__';
const STOCK_PAGE_SIZE_OPTIONS = [20, 50, 100] as const;
const DEFAULT_STOCK_PAGE_SIZE = 50;
type DiscoverMarket = Extract<Market, 'CN' | 'BSE' | 'HK' | 'US'>;
type DiscoverAssetType = 'stock' | 'etf';

const MARKET_OPTIONS: Array<{ value: DiscoverMarket; label: string }> = [
  { value: 'CN', label: '沪深 A 股' },
  { value: 'BSE', label: '北交所' },
  { value: 'HK', label: '港股' },
  { value: 'US', label: '美股' },
];

const STOCK_RANKING_TABS: Array<{
  key: string;
  label: string;
  metric: RankingMetric;
  direction: RankingDirection;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { key: 'gainers', label: '涨幅', metric: 'change_pct', direction: 'desc', icon: TrendingUp },
  { key: 'losers', label: '跌幅', metric: 'change_pct', direction: 'asc', icon: TrendingDown },
  { key: 'amount', label: '成交额', metric: 'amount', direction: 'desc', icon: BarChart3 },
  { key: 'volume', label: '成交量', metric: 'volume', direction: 'desc', icon: LineChart },
];

const ETF_RANKING_TABS: typeof STOCK_RANKING_TABS = [
  { key: 'drawdown', label: '250日回撤', metric: 'drawdown_250d_pct', direction: 'desc', icon: TrendingDown },
  { key: 'return20', label: '20日', metric: 'return_20d_pct', direction: 'asc', icon: LineChart },
  { key: 'return60', label: '60日', metric: 'return_60d_pct', direction: 'asc', icon: LineChart },
  { key: 'return250', label: '250日', metric: 'return_250d_pct', direction: 'asc', icon: BarChart3 },
  { key: 'amount', label: '成交额', metric: 'amount', direction: 'desc', icon: BarChart3 },
  { key: 'volume', label: '成交量', metric: 'volume', direction: 'desc', icon: LineChart },
];
const ETF_HISTORY_METRICS = new Set<RankingMetric>([
  'drawdown_250d_pct', 'return_20d_pct', 'return_60d_pct', 'return_250d_pct',
]);

const ETF_CATEGORY_LABELS: Record<string, string> = {
  broad_market: '全市场核心',
  large_cap: '大盘宽基',
  mid_cap: '中盘宽基',
  small_cap: '小盘宽基',
  growth: '成长宽基',
  innovation: '创新宽基',
};

const etfCategoryLabel = (value?: string | null) => (
  value ? ETF_CATEGORY_LABELS[value] ?? value : '宽基 ETF'
);

const STATUS_META: Record<RankingStatus, { label: string; variant: 'default' | 'success' | 'warning' | 'danger' | 'info' }> = {
  ok: { label: '实时', variant: 'success' },
  partial: { label: '部分', variant: 'warning' },
  stale: { label: '缓存', variant: 'warning' },
  unsupported: { label: '不可用', variant: 'default' },
  unavailable: { label: '失败', variant: 'danger' },
};

type ActionNotice = {
  variant: 'success' | 'warning' | 'danger';
  title: string;
  message: string;
} | null;

const formatNumber = (value?: number | null, options?: Intl.NumberFormatOptions) => {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return new Intl.NumberFormat('zh-CN', options).format(value);
};

const formatAmount = (value?: number | null) => {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  if (Math.abs(value) >= 100000000) return `${formatNumber(value / 100000000, { maximumFractionDigits: 2 })} 亿`;
  if (Math.abs(value) >= 10000) return `${formatNumber(value / 10000, { maximumFractionDigits: 2 })} 万`;
  return formatNumber(value, { maximumFractionDigits: 0 });
};

const formatPct = (value?: number | null) => {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${value > 0 ? '+' : ''}${formatNumber(value, { maximumFractionDigits: 2 })}%`;
};

const formatDrawdown = (value?: number | null) => {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `-${formatNumber(Math.abs(value), { maximumFractionDigits: 2 })}%`;
};

const getErrorMessage = (error: unknown, fallback: string) => {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
};

const getChangeClass = (value?: number | null) => {
  if (value === undefined || value === null) return 'text-secondary-text';
  if (value > 0) return 'text-success';
  if (value < 0) return 'text-danger';
  return 'text-secondary-text';
};

const DiscoverPage: React.FC = () => {
  const navigate = useNavigate();
  const { index, loading, error, fallback } = useStockIndex();
  const [assetType, setAssetType] = useState<DiscoverAssetType>('stock');
  const [market, setMarket] = useState<DiscoverMarket>('CN');
  const [keyword, setKeyword] = useState('');
  const [industry, setIndustry] = useState('');
  const [rankingKey, setRankingKey] = useState('gainers');
  const [rankings, setRankings] = useState<StockRankingItem[]>([]);
  const [rankingStatus, setRankingStatus] = useState<RankingStatus>('unsupported');
  const [rankingUpdatedAt, setRankingUpdatedAt] = useState<string | null>(null);
  const [rankingMessage, setRankingMessage] = useState<string | null>(null);
  const [historyAsOfDate, setHistoryAsOfDate] = useState<string | null>(null);
  const [historyCoverage, setHistoryCoverage] = useState<number | null>(null);
  const [historyTotal, setHistoryTotal] = useState<number | null>(null);
  const [historyStale, setHistoryStale] = useState(false);
  const [rankingLoading, setRankingLoading] = useState(false);
  const [rankingRefreshNonce, setRankingRefreshNonce] = useState(0);
  const [warmingEtfHistory, setWarmingEtfHistory] = useState(false);
  const [rankingError, setRankingError] = useState<string | null>(null);
  const [analyzingCode, setAnalyzingCode] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [stockPage, setStockPage] = useState(1);
  const [stockPageSize, setStockPageSize] = useState(DEFAULT_STOCK_PAGE_SIZE);
  const [kLineStock, setKLineStock] = useState<{ code: string; name: string } | null>(null);
  const [watchlistOnly, setWatchlistOnly] = useState(false);
  const watchlist = useWatchlistConfig({ index });

  const rankingTabs = assetType === 'etf' ? ETF_RANKING_TABS : STOCK_RANKING_TABS;
  const activeRanking = rankingTabs.find((tab) => tab.key === rankingKey) ?? rankingTabs[0];
  const watchlistFilterDisabled = watchlist.loading || watchlist.saving || (!watchlist.hasConfig && !watchlistOnly);

  const marketStocks = useMemo(
    () => index.filter((item) => item.active && item.assetType === assetType && item.market === market),
    [assetType, index, market],
  );

  const keywordFilteredStocks = useMemo(() => {
    const trimmed = keyword.trim();
    if (!trimmed) return marketStocks;
    const matchedCodes = new Set(
      searchStocks(trimmed, index, { activeOnly: true, limit: index.length })
        .map((suggestion) => suggestion.canonicalCode)
    );
    return marketStocks.filter((item) => matchedCodes.has(item.canonicalCode));
  }, [index, keyword, marketStocks]);

  const industryOptions = useMemo(() => {
    const counts = new Map<string, number>();
    let uncategorized = 0;
    for (const item of keywordFilteredStocks) {
      const classification = assetType === 'etf' ? item.etfCategory : item.industry;
      if (classification) {
        counts.set(classification, (counts.get(classification) ?? 0) + 1);
      } else {
        uncategorized += 1;
      }
    }
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
    return [
      { value: '', label: `全部 (${keywordFilteredStocks.length})` },
      ...sorted.map(([value, count]) => ({
        value,
        label: `${assetType === 'etf' ? etfCategoryLabel(value) : value} (${count})`,
      })),
      ...(uncategorized > 0 ? [{ value: UNCATEGORIZED_INDUSTRY, label: `未分类 (${uncategorized})` }] : []),
    ];
  }, [assetType, keywordFilteredStocks]);

  useEffect(() => {
    if (!industryOptions.some((option) => option.value === industry)) {
      setIndustry('');
    }
  }, [industry, industryOptions]);

  const filteredStocks = useMemo(() => {
    const industryFiltered = (() => {
      if (industry === UNCATEGORIZED_INDUSTRY) {
        return keywordFilteredStocks.filter((item) => !(assetType === 'etf' ? item.etfCategory : item.industry));
      }
      if (industry) {
        return keywordFilteredStocks.filter((item) => (
          assetType === 'etf' ? item.etfCategory === industry : item.industry === industry
        ));
      }
      return keywordFilteredStocks;
    })();

    if (watchlistOnly) {
      return industryFiltered.filter((item) => watchlist.isWatchlisted(item.canonicalCode, item.market));
    }
    return industryFiltered;
  }, [assetType, industry, keywordFilteredStocks, watchlist, watchlistOnly]);

  useEffect(() => {
    setStockPage(1);
  }, [assetType, industry, keyword, market, watchlistOnly]);

  const handleToggleWatchlist = useCallback(async (stockCode: string, stockName: string, stockMarket?: Market | null) => {
    setActionNotice(null);
    await watchlist.toggleWatchlist(stockCode, stockName, stockMarket);
  }, [watchlist]);

  const coverage = useMemo(() => {
    const denominator = keywordFilteredStocks.length;
    const numerator = keywordFilteredStocks.filter((item) => Boolean(
      assetType === 'etf' ? item.benchmarkName : item.industry
    )).length;
    return {
      numerator,
      denominator,
      percent: denominator > 0 ? Math.round((numerator / denominator) * 100) : 0,
    };
  }, [assetType, keywordFilteredStocks]);

  useEffect(() => {
    let cancelled = false;
    setRankingLoading(true);
    setRankingError(null);
    setRankingMessage(null);

    stocksApi.getRankings({
      market,
      industry: assetType === 'stock' ? industry || undefined : undefined,
      category: assetType === 'etf' ? industry || undefined : undefined,
      assetType,
      metric: activeRanking.metric,
      direction: activeRanking.direction,
      limit: 20,
    })
      .then((payload) => {
        if (cancelled) return;
        setRankings(payload.items ?? []);
        setRankingStatus(payload.status);
        setRankingUpdatedAt(payload.updatedAt ?? null);
        setRankingMessage(payload.message ?? null);
        setHistoryAsOfDate(payload.historyAsOfDate ?? null);
        setHistoryCoverage(payload.historyCoverage ?? null);
        setHistoryTotal(payload.historyTotal ?? null);
        setHistoryStale(Boolean(payload.historyStale));
      })
      .catch((requestError: unknown) => {
        if (cancelled) return;
        setRankings([]);
        setRankingStatus('unsupported');
        setRankingMessage(null);
        setHistoryAsOfDate(null);
        setHistoryCoverage(null);
        setHistoryTotal(null);
        setHistoryStale(false);
        setRankingError(getErrorMessage(requestError, '榜单暂时不可用'));
      })
      .finally(() => {
        if (!cancelled) setRankingLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [activeRanking.direction, activeRanking.metric, assetType, industry, market, rankingRefreshNonce]);

  const handleAssetTypeChange = useCallback((next: DiscoverAssetType) => {
    setAssetType(next);
    setIndustry('');
    setKeyword('');
    setRankingKey(next === 'etf' ? 'drawdown' : 'gainers');
    if (next === 'etf') setMarket('CN');
  }, []);

  const handleAnalyze = useCallback(async (stockCode: string, stockName: string) => {
    setAnalyzingCode(stockCode);
    setActionNotice(null);
    try {
      const result = await analysisApi.analyzeAsync({
        stockCode,
        stockName,
        originalQuery: stockCode,
        selectionSource: 'discover',
        asyncMode: true,
      });
      const taskId = 'taskId' in result ? result.taskId : result.accepted?.[0]?.taskId;
      setActionNotice({
        variant: 'success',
        title: '已提交分析',
        message: taskId ? `任务 ${taskId} 已进入队列` : `${stockName} 已进入分析队列`,
      });
    } catch (requestError) {
      if (requestError instanceof DuplicateTaskError) {
        setActionNotice({
          variant: 'warning',
          title: '分析已在进行',
          message: requestError.message,
        });
      } else {
        setActionNotice({
          variant: 'danger',
          title: '提交失败',
          message: getErrorMessage(requestError, '暂时无法提交分析任务'),
        });
      }
    } finally {
      setAnalyzingCode(null);
    }
  }, []);

  const handleAsk = useCallback((stockCode: string, stockName: string) => {
    navigate(`/chat?stock=${encodeURIComponent(stockCode)}&name=${encodeURIComponent(stockName)}`);
  }, [navigate]);

  const handleOpenKLine = useCallback((stockCode: string, stockName: string) => {
    setKLineStock({ code: stockCode, name: stockName });
  }, []);

  const handleWarmEtfHistory = useCallback(async () => {
    setWarmingEtfHistory(true);
    setActionNotice(null);
    try {
      const result = await stocksApi.warmEtfHistory(true);
      const complete = result.status === 'ok';
      setActionNotice({
        variant: complete ? 'success' : 'warning',
        title: complete ? 'ETF 历史行情已预热' : 'ETF 历史行情部分可用',
        message: `成功 ${result.succeeded}/${result.total}，旧缓存 ${result.stale}，失败 ${result.failed}`,
      });
      setRankingRefreshNonce((value) => value + 1);
    } catch (requestError) {
      setActionNotice({
        variant: 'danger',
        title: 'ETF 历史行情预热失败',
        message: getErrorMessage(requestError, '暂时无法刷新 ETF 日线'),
      });
    } finally {
      setWarmingEtfHistory(false);
    }
  }, []);

  const handleCreatePlan = useCallback((item: Pick<StockRankingItem, 'code' | 'name' | 'benchmarkCode'>) => {
    const params = new URLSearchParams({
      symbol: item.code.split('.')[0],
      name: item.name,
      market: 'CN',
      strategyType: 'index_crash',
      benchmarkSymbol: item.code.split('.')[0],
    });
    navigate(`/plans?${params.toString()}`);
  }, [navigate]);

  const handleStockPageSizeChange = useCallback((value: string) => {
    const parsed = Number(value);
    const nextSize = STOCK_PAGE_SIZE_OPTIONS.find((option) => option === parsed) ?? DEFAULT_STOCK_PAGE_SIZE;
    setStockPageSize(nextSize);
    setStockPage(1);
  }, []);

  const totalStockPages = Math.max(1, Math.ceil(filteredStocks.length / stockPageSize));
  const clampedStockPage = Math.min(stockPage, totalStockPages);
  const pageStartIndex = (clampedStockPage - 1) * stockPageSize;
  const visibleStocks = filteredStocks.slice(pageStartIndex, pageStartIndex + stockPageSize);
  const visibleStart = filteredStocks.length > 0 ? pageStartIndex + 1 : 0;
  const visibleEnd = pageStartIndex + visibleStocks.length;
  const statusMeta = STATUS_META[rankingStatus];
  const rankingEmptyTitle = rankingStatus === 'unsupported'
    ? '暂无榜单数据'
    : rankingStatus === 'unavailable'
      ? '行情源不可用'
      : '没有匹配榜单';
  const rankingEmptyDescription = rankingStatus === 'unavailable'
    ? rankingMessage || '批量行情源暂不可用，且没有可用缓存'
    : '';

  return (
    <AppPage data-testid="discover-page" className="max-w-[2160px] space-y-4">
      <section data-testid="discover-compact-toolbar" data-slot="toolbar" className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-xs font-medium text-muted-foreground">市场研究</span>
              <h1 className="text-2xl font-semibold tracking-tight text-foreground">标的发现</h1>
              <div className="inline-flex rounded-md border border-border bg-background p-0.5" aria-label="标的类型">
                {([
                  ['stock', '股票'],
                  ['etf', '宽基 ETF'],
                ] as const).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={assetType === value}
                    onClick={() => handleAssetTypeChange(value)}
                    className={cn(
                      'rounded px-2.5 py-1 text-xs font-medium transition-colors',
                      assetType === value
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                aria-pressed={watchlistOnly}
                aria-label="只看自选"
                disabled={watchlistFilterDisabled}
                onClick={() => setWatchlistOnly((value) => !value)}
                className={cn(
                  'inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors',
                  watchlistOnly
                    ? 'border-warning/35 bg-warning/12 text-warning'
                    : 'border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
                  watchlistFilterDisabled ? 'cursor-not-allowed opacity-50' : ''
                )}
              >
                <Star className="h-3.5 w-3.5" fill={watchlistOnly ? 'currentColor' : 'none'} />
                只看自选
              </button>
              <Badge variant="info">{assetType === 'etf' ? 'A 股宽基精选池' : MARKET_OPTIONS.find((option) => option.value === market)?.label}</Badge>
              <Badge variant={coverage.percent >= 60 ? 'success' : coverage.percent > 0 ? 'warning' : 'default'}>
                {assetType === 'etf' ? '指数信息' : '行业覆盖'} {coverage.numerator}/{coverage.denominator}
              </Badge>
            </div>
          </div>

          <div
            data-testid="discover-filter-grid"
            className="grid gap-3 md:grid-cols-[160px_minmax(220px,1fr)_220px] xl:grid-cols-[160px_minmax(260px,1fr)_220px_minmax(360px,0.7fr)] xl:items-end 2xl:grid-cols-[160px_minmax(260px,720px)_220px_minmax(360px,1fr)]"
          >
            <Select
              label="市场"
              value={market}
              onChange={(value) => setMarket(value as DiscoverMarket)}
              options={MARKET_OPTIONS}
              disabled={assetType === 'etf'}
            />
            <div data-testid="discover-search-field" className="w-full 2xl:max-w-[720px]">
              <Input
                label="关键词"
                type="search"
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
                placeholder="代码、名称、拼音、别名"
                trailingAction={<Search className="h-4 w-4 text-muted-foreground" />}
              />
            </div>
            <Select
              label={assetType === 'etf' ? '宽基类型' : '行业'}
              value={industry}
              onChange={setIndustry}
              options={industryOptions}
              disabled={loading || industryOptions.length <= 1}
            />
            <div data-testid="discover-compact-metrics" className="grid grid-cols-3 gap-2 md:col-span-3 xl:col-span-1">
              <CompactMetric label={assetType === 'etf' ? '精选 ETF' : '当前市场'} value={formatNumber(marketStocks.length)} />
              <CompactMetric label="当前结果" value={formatNumber(filteredStocks.length)} />
              <CompactMetric label={assetType === 'etf' ? '指数信息率' : '行业覆盖率'} value={`${coverage.percent}%`} />
            </div>
          </div>
        </div>
      </section>

      {error || fallback ? (
        <InlineAlert
          variant="warning"
          title="静态索引未完整加载"
          message={error?.message || '当前仅能显示已加载的数据'}
        />
      ) : null}

      {watchlist.error ? (
        <InlineAlert
          variant="warning"
          title="自选配置不可用"
          message={`${watchlist.error}。发现页仍可浏览、分析、问股和查看 K 线。`}
        />
      ) : null}

      <FloatingActionToast notice={actionNotice ?? watchlist.notice} />

      <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            {rankingTabs.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => setRankingKey(key)}
                className={cn(
                  'inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors',
                  rankingKey === key
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground'
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {assetType === 'etf' ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void handleWarmEtfHistory()}
                isLoading={warmingEtfHistory}
                loadingText="预热中"
                aria-label="立即预热 ETF 历史行情"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                立即预热
              </Button>
            ) : null}
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
            {market === 'US' ? <Badge variant="info">核心池</Badge> : null}
            {assetType === 'etf' ? <Badge variant="info">本地精选 · 动态行情</Badge> : null}
            {historyCoverage !== null && historyTotal !== null ? (
              <Badge variant={historyStale ? 'warning' : 'success'}>
                历史 {historyCoverage}/{historyTotal}
              </Badge>
            ) : null}
            {historyAsOfDate ? <span>历史截至 {historyAsOfDate}</span> : null}
            {rankingUpdatedAt ? <span>{new Date(rankingUpdatedAt).toLocaleString('zh-CN')}</span> : null}
          </div>
        </div>

        {rankingError ? (
          <div className="mt-4">
            <InlineAlert variant="warning" title="榜单不可用" message={rankingError} />
          </div>
        ) : null}

        <div className="mt-4 grid gap-3 lg:grid-cols-2 xl:grid-cols-4">
          {rankingLoading ? (
            Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="h-28 animate-pulse rounded-lg border border-border bg-muted/50" />
            ))
          ) : rankings.length > 0 ? (
            rankings.slice(0, 8).map((item, index) => (
              <RankingTile
                key={`${item.code}-${index}`}
                item={item}
                rank={index + 1}
                rankingMetric={activeRanking.metric}
                onAnalyze={handleAnalyze}
                onAsk={handleAsk}
                onOpenKLine={handleOpenKLine}
                onToggleWatchlist={handleToggleWatchlist}
                onCreatePlan={assetType === 'etf' ? handleCreatePlan : undefined}
                isWatchlisted={watchlist.isWatchlisted(item.code, item.market)}
                watchlistDisabled={watchlist.disabled}
                watchlistSaving={watchlist.isSavingStock(item.code, item.market)}
                analyzingCode={analyzingCode}
              />
            ))
          ) : (
            <div className="lg:col-span-2 xl:col-span-4">
              <EmptyState
                title={rankingEmptyTitle}
                description={rankingEmptyDescription}
                icon={<AlertCircle className="h-6 w-6" />}
              />
            </div>
          )}
        </div>
      </section>

      <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-base font-semibold text-foreground">{assetType === 'etf' ? '宽基 ETF 精选池' : '可关注股票'}</h2>
            <span className="mt-1 block text-xs text-muted-foreground">
              {filteredStocks.length > 0
                ? `显示 ${visibleStart}-${visibleEnd} / ${filteredStocks.length}`
                : '0 只'}
            </span>
          </div>
          <Select
            label="每页"
            value={String(stockPageSize)}
            onChange={handleStockPageSizeChange}
            options={STOCK_PAGE_SIZE_OPTIONS.map((size) => ({ value: String(size), label: `${size} 条` }))}
            className="w-full sm:w-32"
            disabled={loading || filteredStocks.length === 0}
          />
        </div>

        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="h-12 animate-pulse rounded-lg bg-muted/50" />
            ))}
          </div>
        ) : visibleStocks.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-border bg-background">
            <div data-testid="discover-stock-table-scroll" data-slot="data-table" role="region" aria-label={assetType === 'etf' ? '宽基 ETF 列表' : '可关注股票列表'} className="max-h-[520px] overflow-auto">
              <table className="min-w-[940px] w-full text-left text-sm">
                <thead className="sticky top-0 z-10 border-b border-border bg-muted/90 text-xs text-muted-foreground backdrop-blur">
                  <tr>
                    <th className="px-3 py-2 font-medium">代码</th>
                    <th className="px-3 py-2 font-medium">名称</th>
                    <th className="px-3 py-2 font-medium">市场</th>
                    <th className="px-3 py-2 font-medium">{assetType === 'etf' ? '跟踪指数 / 类型' : '行业'}</th>
                    <th className="w-[340px] px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {visibleStocks.map((item) => (
                    <tr key={item.canonicalCode} className="transition-colors hover:bg-muted/60">
                      <td className="px-3 py-3 font-mono text-sm text-foreground">{item.displayCode}</td>
                      <td className="px-3 py-3">
                        <div className="font-medium text-foreground">{item.nameZh}</div>
                        <div className="text-xs text-muted-foreground">{item.canonicalCode}</div>
                      </td>
                      <td className="px-3 py-3 text-muted-foreground">
                        {MARKET_OPTIONS.find((option) => option.value === item.market)?.label ?? item.market}
                      </td>
                      <td className="px-3 py-3">
                        {assetType === 'etf' ? (
                          <div>
                            <div className="text-sm font-medium text-foreground">{item.benchmarkName || '指数待补充'}</div>
                            <div className="mt-1 text-xs text-muted-foreground">{etfCategoryLabel(item.etfCategory)}</div>
                          </div>
                        ) : (
                          <Badge variant={item.industry ? 'info' : 'default'}>
                            {item.industry || '未分类'}
                          </Badge>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-1.5">
                          <WatchlistStarButton
                            stockName={item.nameZh}
                            isStarred={watchlist.isWatchlisted(item.canonicalCode, item.market)}
                            disabled={watchlist.disabled}
                            isSaving={watchlist.isSavingStock(item.canonicalCode, item.market)}
                            onClick={() => void handleToggleWatchlist(item.canonicalCode, item.nameZh, item.market)}
                          />
                          <Tooltip content="查看 K 线">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="w-9 px-0"
                              onClick={() => handleOpenKLine(item.canonicalCode, item.nameZh)}
                              aria-label={`查看 ${item.nameZh} K线`}
                            >
                              <ChartCandlestick className="h-4 w-4" />
                            </Button>
                          </Tooltip>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => void handleAnalyze(item.canonicalCode, item.nameZh)}
                            isLoading={analyzingCode === item.canonicalCode}
                            loadingText="分析中"
                          >
                            <Play className="h-4 w-4" />
                            分析
                          </Button>
                          {assetType === 'etf' ? (
                            <Tooltip content="制定大跌分批买入计划">
                              <Button
                                size="sm"
                                variant="ghost"
                                className="w-9 px-0"
                                aria-label={`从列表为 ${item.nameZh} 制定计划`}
                                onClick={() => handleCreatePlan({
                                  code: item.canonicalCode,
                                  name: item.nameZh,
                                  benchmarkCode: item.benchmarkCode,
                                })}
                              >
                                <Target className="h-4 w-4" />
                              </Button>
                            </Tooltip>
                          ) : null}
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => handleAsk(item.canonicalCode, item.nameZh)}
                          >
                            <MessageSquareQuote className="h-4 w-4" />
                            问股
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {totalStockPages > 1 ? (
              <div className="border-t border-border bg-muted/30 px-3 py-3">
                <Pagination
                  currentPage={clampedStockPage}
                  totalPages={totalStockPages}
                  onPageChange={setStockPage}
                  className="justify-end"
                />
              </div>
            ) : null}
          </div>
        ) : (
          <EmptyState title={assetType === 'etf' ? '没有匹配宽基 ETF' : '没有匹配股票'} description="" icon={<Search className="h-6 w-6" />} />
        )}
      </section>

      <StockKLineDrawer
        isOpen={Boolean(kLineStock)}
        stockCode={kLineStock?.code}
        stockName={kLineStock?.name}
        onClose={() => setKLineStock(null)}
      />
    </AppPage>
  );
};

type CompactMetricProps = {
  label: string;
  value: string;
};

const CompactMetric: React.FC<CompactMetricProps> = ({ label, value }) => (
  <div data-slot="stat-card" className="min-w-0 rounded-md border border-border bg-background px-3 py-2">
    <div className="truncate text-[11px] leading-4 text-muted-foreground">{label}</div>
    <div className="truncate text-sm font-semibold leading-5 text-foreground">{value}</div>
  </div>
);

type FloatingActionToastProps = {
  notice: ActionNotice;
};

const TOAST_VARIANT_STYLES: Record<NonNullable<ActionNotice>['variant'], string> = {
  success: 'border-success/30 bg-success/12 text-success shadow-success/10',
  warning: 'border-warning/35 bg-warning/12 text-warning shadow-warning/10',
  danger: 'border-danger/35 bg-danger/12 text-danger shadow-danger/10',
};

const FloatingActionToast: React.FC<FloatingActionToastProps> = ({ notice }) => {
  if (!notice) {
    return null;
  }

  return (
    <div
      data-testid="discover-action-toast"
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed left-1/2 top-5 z-[95] w-[min(calc(100vw-2rem),34rem)] -translate-x-1/2 sm:left-auto sm:right-6 sm:translate-x-0"
    >
      <div
        className={cn(
          'rounded-lg border px-4 py-3 shadow-lg',
          TOAST_VARIANT_STYLES[notice.variant],
        )}
      >
        <div className="text-sm font-semibold leading-5">{notice.title}</div>
        <div className="mt-1 text-sm leading-5 opacity-90">{notice.message}</div>
      </div>
    </div>
  );
};

type RankingTileProps = {
  item: StockRankingItem;
  rank: number;
  rankingMetric: RankingMetric;
  analyzingCode: string | null;
  onAnalyze: (stockCode: string, stockName: string) => Promise<void>;
  onAsk: (stockCode: string, stockName: string) => void;
  onOpenKLine: (stockCode: string, stockName: string) => void;
  onToggleWatchlist: (stockCode: string, stockName: string, stockMarket: Market) => Promise<void>;
  onCreatePlan?: (item: Pick<StockRankingItem, 'code' | 'name' | 'benchmarkCode'>) => void;
  isWatchlisted: boolean;
  watchlistDisabled: boolean;
  watchlistSaving: boolean;
};

const RankingTile: React.FC<RankingTileProps> = ({
  item,
  rank,
  rankingMetric,
  analyzingCode,
  onAnalyze,
  onAsk,
  onOpenKLine,
  onToggleWatchlist,
  onCreatePlan,
  isWatchlisted,
  watchlistDisabled,
  watchlistSaving,
}) => (
  <div className="rounded-lg border border-border/55 bg-elevated/35 p-4 transition-colors hover:bg-hover/60">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-semibold text-primary-foreground">
            {rank}
          </span>
          <span className="truncate text-sm font-semibold text-foreground">{item.name}</span>
        </div>
        <div className="mt-1 font-mono text-xs text-muted-text">{item.code}</div>
      </div>
      <div className={cn('shrink-0 text-right text-sm font-semibold', getChangeClass(item.changePct))}>
        {formatPct(item.changePct)}
      </div>
    </div>
    <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
      {item.assetType === 'etf' ? (
        <>
          <ValueCell label="250日回撤" value={formatDrawdown(item.drawdown250dPct)} />
          <ValueCell label="20日收益" value={formatPct(item.return20dPct)} />
          <ValueCell
            label={ETF_HISTORY_METRICS.has(rankingMetric) ? '数据截至' : rankingMetric === 'volume' ? '成交量' : '成交额'}
            value={rankingMetric === 'volume'
              ? formatAmount(item.volume)
              : ETF_HISTORY_METRICS.has(rankingMetric)
                ? item.historyAsOfDate || '-'
                : formatAmount(item.amount)}
          />
        </>
      ) : (
        <>
          <ValueCell label="价格" value={formatNumber(item.price, { maximumFractionDigits: 3 })} />
          <ValueCell label="成交额" value={formatAmount(item.amount)} />
          <ValueCell label="成交量" value={formatAmount(item.volume)} />
        </>
      )}
    </div>
    <div className="mt-3 flex items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={item.assetType === 'etf' || item.industry ? 'info' : 'default'}>
          {item.assetType === 'etf' ? `${item.benchmarkName || '宽基指数'} · ${etfCategoryLabel(item.category)}` : item.industry || '未分类'}
        </Badge>
        {item.assetType === 'etf' && item.historyStale && (
          <Badge variant="warning">旧缓存</Badge>
        )}
      </div>
      <div className="flex gap-1.5">
        <WatchlistStarButton
          stockName={item.name}
          isStarred={isWatchlisted}
          disabled={watchlistDisabled}
          isSaving={watchlistSaving}
          size="xsm"
          onClick={() => void onToggleWatchlist(item.code, item.name, item.market)}
        />
        <Tooltip content="查看 K 线">
          <Button
            size="xsm"
            variant="ghost"
            onClick={() => onOpenKLine(item.code, item.name)}
            aria-label={`查看 ${item.name} K线`}
          >
            <ChartCandlestick className="h-3.5 w-3.5" />
          </Button>
        </Tooltip>
        <Button
          size="xsm"
          variant="secondary"
          onClick={() => void onAnalyze(item.code, item.name)}
          isLoading={analyzingCode === item.code}
          loadingText=""
          aria-label={`分析 ${item.name}`}
        >
          <Play className="h-3.5 w-3.5" />
        </Button>
        <Button
          size="xsm"
          variant="ghost"
          onClick={() => onAsk(item.code, item.name)}
          aria-label={`问股 ${item.name}`}
        >
          <MessageSquareQuote className="h-3.5 w-3.5" />
        </Button>
        {onCreatePlan ? (
          <Tooltip content="制定大跌分批买入计划">
            <Button
              size="xsm"
              variant="secondary"
              onClick={() => onCreatePlan(item)}
              aria-label={`为 ${item.name} 制定计划`}
            >
              <Target className="h-3.5 w-3.5" />
            </Button>
          </Tooltip>
        ) : null}
      </div>
    </div>
  </div>
);

type ValueCellProps = {
  label: string;
  value: string;
};

const ValueCell: React.FC<ValueCellProps> = ({ label, value }) => (
  <div className="min-w-0 rounded-md bg-base/35 px-2 py-2">
    <div className="truncate text-muted-text">{label}</div>
    <div className="mt-1 truncate font-medium text-foreground">{value}</div>
  </div>
);

export default DiscoverPage;
