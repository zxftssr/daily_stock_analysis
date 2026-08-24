import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChartCandlestick,
  Filter,
  MessageSquareQuote,
  ListChecks,
  Play,
  Search,
  ShieldAlert,
  Star,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { analysisApi, DuplicateTaskError } from '../api/analysis';
import {
  stocksApi,
  type RankingDirection,
  type RankingMetric,
} from '../api/stocks';
import { AppPage, Badge, Button, EmptyState, InlineAlert, Input, Pagination, Select, Tooltip } from '../components/common';
import { StockKLineDrawer } from '../components/stocks/StockKLineDrawer';
import { WatchlistStarButton } from '../components/stocks/WatchlistStarButton';
import { useStockIndex } from '../hooks/useStockIndex';
import { useWatchlistConfig, type WatchlistNotice } from '../hooks/useWatchlistConfig';
import type { Market } from '../types/stockIndex';
import {
  buildCandidatePool,
  type CandidateMode,
  type CandidatePoolStatus,
  type CandidateQuoteStatus,
  type CandidateRankingSignal,
  type CandidateSignalKey,
} from '../utils/candidateScoring';
import { cn } from '../utils/cn';
import { searchStocks } from '../utils/searchStocks';

const UNCATEGORIZED_INDUSTRY = '__uncategorized__';
const PAGE_SIZE = 20;
const RANKING_CACHE_TTL_MS = 5 * 60 * 1000;
const RANKING_CACHE_PREFIX = 'dsa:candidate-rankings:v1';

type CandidateMarket = Extract<Market, 'CN' | 'BSE' | 'HK' | 'US'>;

const MARKET_OPTIONS: Array<{ value: CandidateMarket; label: string }> = [
  { value: 'CN', label: '沪深 A 股' },
  { value: 'BSE', label: '北交所' },
  { value: 'HK', label: '港股' },
  { value: 'US', label: '美股' },
];

const MODE_OPTIONS: Array<{ value: CandidateMode; label: string }> = [
  { value: 'balanced', label: '综合关注' },
  { value: 'momentum', label: '趋势活跃' },
  { value: 'liquidity', label: '成交活跃' },
  { value: 'pullback', label: '回调观察' },
];

const MODE_REQUESTS: Record<CandidateMode, Array<{
  key: CandidateSignalKey;
  metric: RankingMetric;
  direction: RankingDirection;
}>> = {
  balanced: [
    { key: 'amount', metric: 'amount', direction: 'desc' },
    { key: 'gainers', metric: 'change_pct', direction: 'desc' },
    { key: 'volume', metric: 'volume', direction: 'desc' },
  ],
  momentum: [
    { key: 'gainers', metric: 'change_pct', direction: 'desc' },
    { key: 'amount', metric: 'amount', direction: 'desc' },
    { key: 'volume', metric: 'volume', direction: 'desc' },
  ],
  liquidity: [
    { key: 'amount', metric: 'amount', direction: 'desc' },
    { key: 'volume', metric: 'volume', direction: 'desc' },
  ],
  pullback: [
    { key: 'losers', metric: 'change_pct', direction: 'asc' },
    { key: 'amount', metric: 'amount', direction: 'desc' },
    { key: 'volume', metric: 'volume', direction: 'desc' },
  ],
};

const STATUS_META: Record<CandidatePoolStatus, { label: string; variant: 'default' | 'success' | 'warning' | 'danger' | 'info' }> = {
  live: { label: '实时', variant: 'success' },
  partial: { label: '部分行情', variant: 'warning' },
  cached: { label: '缓存行情', variant: 'warning' },
  static: { label: '静态候选', variant: 'default' },
  empty: { label: '无候选', variant: 'default' },
};

const QUOTE_STATUS_META: Record<CandidateQuoteStatus, { label: string; variant: 'default' | 'success' | 'warning' | 'danger' | 'info' }> = {
  live: { label: '实时', variant: 'success' },
  cached: { label: '缓存', variant: 'warning' },
  static: { label: '静态', variant: 'default' },
};

type ActionNotice = WatchlistNotice;

type CandidateRankingCachePayload = {
  cachedAt: number;
  signals: CandidateRankingSignal[];
};

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

const getChangeClass = (value?: number | null) => {
  if (value === undefined || value === null) return 'text-secondary-text';
  if (value > 0) return 'text-success';
  if (value < 0) return 'text-danger';
  return 'text-secondary-text';
};

const getErrorMessage = (error: unknown, fallback: string) => {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
};

let rankingRequestSeq = 0;

const buildRankingCacheKey = (market: CandidateMarket, industry: string, mode: CandidateMode) => (
  `${RANKING_CACHE_PREFIX}:${market}:${encodeURIComponent(industry)}:${mode}`
);

const readRankingCache = (cacheKey: string): CandidateRankingSignal[] | null => {
  try {
    const raw = window.sessionStorage.getItem(cacheKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CandidateRankingCachePayload>;
    const cachedAt = parsed.cachedAt;
    if (typeof cachedAt !== 'number' || !Number.isFinite(cachedAt) || !Array.isArray(parsed.signals)) return null;
    if (Date.now() - cachedAt > RANKING_CACHE_TTL_MS) return null;
    return parsed.signals.map((signal) => ({
      ...signal,
      status: signal.items.length > 0 ? 'stale' : signal.status,
    }));
  } catch {
    return null;
  }
};

const writeRankingCache = (cacheKey: string, signals: CandidateRankingSignal[]) => {
  try {
    const payload: CandidateRankingCachePayload = {
      cachedAt: Date.now(),
      signals,
    };
    window.sessionStorage.setItem(cacheKey, JSON.stringify(payload));
  } catch {
    // Best-effort UI cache only; ignore storage quota/private mode failures.
  }
};

const CandidatePoolPage: React.FC = () => {
  const navigate = useNavigate();
  const { index, loading, error, fallback } = useStockIndex();
  const [market, setMarket] = useState<CandidateMarket>('CN');
  const [keyword, setKeyword] = useState('');
  const [industry, setIndustry] = useState('');
  const [mode, setMode] = useState<CandidateMode>('balanced');
  const [hideWatchlisted, setHideWatchlisted] = useState(false);
  const [signals, setSignals] = useState<CandidateRankingSignal[]>([]);
  const [rankingLoading, setRankingLoading] = useState(false);
  const [rankingError, setRankingError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [analyzingCode, setAnalyzingCode] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [kLineStock, setKLineStock] = useState<{ code: string; name: string } | null>(null);
  const requestAbortRef = useRef<AbortController | null>(null);
  const watchlist = useWatchlistConfig({ index });
  const isWatchlisted = watchlist.isWatchlisted;

  const marketStocks = useMemo(
    () => index.filter((item) => item.active && item.assetType === 'stock' && item.market === market),
    [index, market],
  );

  const keywordFilteredStocks = useMemo(() => {
    const trimmed = keyword.trim();
    if (!trimmed) return marketStocks;
    const matchedCodes = new Set(
      searchStocks(trimmed, index, { activeOnly: true, limit: index.length })
        .map((suggestion) => suggestion.canonicalCode),
    );
    return marketStocks.filter((item) => matchedCodes.has(item.canonicalCode));
  }, [index, keyword, marketStocks]);

  const industryOptions = useMemo(() => {
    const counts = new Map<string, number>();
    let uncategorized = 0;
    for (const item of keywordFilteredStocks) {
      if (item.industry) {
        counts.set(item.industry, (counts.get(item.industry) ?? 0) + 1);
      } else {
        uncategorized += 1;
      }
    }
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
    return [
      { value: '', label: `全部 (${keywordFilteredStocks.length})` },
      ...sorted.map(([value, count]) => ({ value, label: `${value} (${count})` })),
      ...(uncategorized > 0 ? [{ value: UNCATEGORIZED_INDUSTRY, label: `未分类 (${uncategorized})` }] : []),
    ];
  }, [keywordFilteredStocks]);

  useEffect(() => {
    if (!industryOptions.some((option) => option.value === industry)) {
      setIndustry('');
    }
  }, [industry, industryOptions]);

  const staticUniverse = useMemo(() => {
    if (industry === UNCATEGORIZED_INDUSTRY) {
      return keywordFilteredStocks.filter((item) => !item.industry);
    }
    if (industry) {
      return keywordFilteredStocks.filter((item) => item.industry === industry);
    }
    return keywordFilteredStocks;
  }, [industry, keywordFilteredStocks]);

  const staticCandidates = useMemo(() => {
    if (hideWatchlisted) {
      return staticUniverse.filter((item) => !isWatchlisted(item.canonicalCode, item.market));
    }
    return staticUniverse;
  }, [hideWatchlisted, isWatchlisted, staticUniverse]);

  useEffect(() => {
    setPage(1);
  }, [hideWatchlisted, industry, keyword, market, mode]);

  useEffect(() => {
    requestAbortRef.current?.abort();
    setSignals([]);
    setRankingError(null);

    if (staticUniverse.length === 0) {
      setRankingLoading(false);
      return;
    }

    const cacheKey = buildRankingCacheKey(market, industry, mode);
    const cachedSignals = readRankingCache(cacheKey);
    if (cachedSignals) {
      setSignals(cachedSignals);
    }

    const controller = new AbortController();
    requestAbortRef.current = controller;
    const requestId = ++rankingRequestSeq;
    setRankingLoading(true);

    const loadSignals = async () => {
      const requests = MODE_REQUESTS[mode];
      const loadedSignals: CandidateRankingSignal[] = [];

      for (let index = 0; index < requests.length; index += 1) {
        const request = requests[index];
        const response = await stocksApi.getRankings({
          market,
          industry: industry || undefined,
          metric: request.metric,
          direction: request.direction,
          limit: 100,
        }, controller.signal);

        const signal: CandidateRankingSignal = {
          key: request.key,
          status: response.status,
          items: response.items ?? [],
        };
        loadedSignals.push(signal);

        if (
          index === 0
          && signal.items.length === 0
          && (signal.status === 'unavailable' || signal.status === 'unsupported')
        ) {
          break;
        }
      }

      if (requestId === rankingRequestSeq && !controller.signal.aborted) {
        setSignals(loadedSignals);
        writeRankingCache(cacheKey, loadedSignals);
      }
    };

    loadSignals()
      .catch((requestError: unknown) => {
        if (requestId !== rankingRequestSeq || controller.signal.aborted) return;
        setSignals([]);
        setRankingError(getErrorMessage(requestError, '候选行情暂时不可用'));
      })
      .finally(() => {
        if (requestId === rankingRequestSeq && !controller.signal.aborted) {
          setRankingLoading(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [industry, market, mode, staticUniverse.length]);

  const candidatePool = useMemo(() => buildCandidatePool({
    stocks: staticCandidates,
    signals,
    mode,
    isWatchlisted,
  }), [isWatchlisted, mode, signals, staticCandidates]);

  const statusMeta = STATUS_META[candidatePool.status];
  const totalPages = Math.max(1, Math.ceil(candidatePool.items.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages);
  const pageStart = (clampedPage - 1) * PAGE_SIZE;
  const visibleCandidates = candidatePool.items.slice(pageStart, pageStart + PAGE_SIZE);
  const visibleStart = candidatePool.items.length > 0 ? pageStart + 1 : 0;
  const visibleEnd = pageStart + visibleCandidates.length;

  const handleToggleWatchlist = useCallback(async (stockCode: string, stockName: string, stockMarket?: Market | null) => {
    setActionNotice(null);
    await watchlist.toggleWatchlist(stockCode, stockName, stockMarket);
  }, [watchlist]);

  const handleAnalyze = useCallback(async (stockCode: string, stockName: string) => {
    setAnalyzingCode(stockCode);
    setActionNotice(null);
    watchlist.setNotice(null);
    try {
      const result = await analysisApi.analyzeAsync({
        stockCode,
        stockName,
        originalQuery: stockCode,
        selectionSource: 'candidate_pool',
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
  }, [watchlist]);

  const handleAsk = useCallback((stockCode: string, stockName: string) => {
    navigate(`/chat?stock=${encodeURIComponent(stockCode)}&name=${encodeURIComponent(stockName)}`);
  }, [navigate]);

  const handleOpenKLine = useCallback((stockCode: string, stockName: string) => {
    setKLineStock({ code: stockCode, name: stockName });
  }, []);

  const handleCreatePlan = useCallback((stockCode: string, stockName: string, stockMarket: Market) => {
    navigate(
      `/plans?symbol=${encodeURIComponent(stockCode)}`
      + `&name=${encodeURIComponent(stockName)}`
      + `&market=${encodeURIComponent(stockMarket)}`,
    );
  }, [navigate]);

  return (
    <AppPage data-testid="candidate-page" className="max-w-[2160px] space-y-4">
      <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-muted-foreground">规则选股</span>
              <Badge variant="info">规则评分</Badge>
            </div>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight text-foreground">推荐关注</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              基于静态股票库和行情榜单生成候选池，仅用于发现值得进一步查看的标的，不构成投资建议。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
            {rankingLoading ? <Badge variant="info">加载行情</Badge> : null}
            <Badge variant="default">{candidatePool.items.length} 只候选</Badge>
          </div>
        </div>
      </section>

      <section data-testid="candidate-toolbar" data-slot="toolbar" className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div
          data-testid="candidate-filter-grid"
          className="grid gap-3 md:grid-cols-[160px_minmax(220px,1fr)_220px_180px] xl:grid-cols-[160px_minmax(260px,1fr)_220px_180px_auto] xl:items-end 2xl:grid-cols-[160px_minmax(260px,720px)_220px_180px_auto] 2xl:justify-start"
        >
          <Select
            label="市场"
            value={market}
            onChange={(value) => setMarket(value as CandidateMarket)}
            options={MARKET_OPTIONS}
          />
          <div data-testid="candidate-search-field" className="w-full 2xl:max-w-[720px]">
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
            label="行业"
            value={industry}
            onChange={setIndustry}
            options={industryOptions}
            disabled={loading || industryOptions.length <= 1}
          />
          <Select
            label="候选模式"
            value={mode}
            onChange={(value) => setMode(value as CandidateMode)}
            options={MODE_OPTIONS}
          />
          <button
            type="button"
            aria-pressed={hideWatchlisted}
            aria-label="隐藏已自选"
            onClick={() => setHideWatchlisted((value) => !value)}
            className={cn(
              'inline-flex h-10 items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors',
              hideWatchlisted
                ? 'border-warning/35 bg-warning/12 text-warning'
                : 'border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            <Star className="h-4 w-4" fill={hideWatchlisted ? 'currentColor' : 'none'} />
            隐藏已自选
          </button>
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
          message={`${watchlist.error}。候选池仍可浏览、分析、问股和查看 K 线。`}
        />
      ) : null}

      {rankingError ? (
        <InlineAlert variant="warning" title="候选行情不可用" message={`${rankingError}。当前按静态候选展示。`} />
      ) : null}

      <FloatingActionToast notice={actionNotice ?? watchlist.notice} />

      <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="mb-3 flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-base font-semibold text-foreground">候选股票</h2>
            <span className="mt-1 block text-xs text-muted-foreground">
              {candidatePool.items.length > 0
                ? `显示 ${visibleStart}-${visibleEnd} / ${candidatePool.items.length}`
                : '0 只'}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Filter className="h-4 w-4" />
            <span>{MARKET_OPTIONS.find((option) => option.value === market)?.label}</span>
            <span>{MODE_OPTIONS.find((option) => option.value === mode)?.label}</span>
          </div>
        </div>

        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="h-14 animate-pulse rounded-lg bg-muted/50" />
            ))}
          </div>
        ) : visibleCandidates.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-border bg-background">
            <div data-testid="candidate-table-scroll" data-slot="data-table" role="region" aria-label="候选股票列表" className="max-h-[620px] overflow-auto">
              <table className="min-w-[1280px] w-full text-left text-sm">
                <thead className="sticky top-0 z-10 border-b border-border bg-muted/90 text-xs text-muted-foreground backdrop-blur">
                  <tr>
                    <th className="px-3 py-2 font-medium">关注分</th>
                    <th className="px-3 py-2 font-medium">股票</th>
                    <th className="px-3 py-2 font-medium">市场</th>
                    <th className="px-3 py-2 font-medium">行业</th>
                    <th className="px-3 py-2 text-right font-medium">价格</th>
                    <th className="px-3 py-2 text-right font-medium">涨跌幅</th>
                    <th className="px-3 py-2 text-right font-medium">成交额</th>
                    <th className="px-3 py-2 text-right font-medium">成交量</th>
                    <th className="px-3 py-2 font-medium">行情状态</th>
                    <th className="px-3 py-2 font-medium">理由</th>
                    <th className="w-[326px] px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {visibleCandidates.map((item) => (
                    <tr key={item.code} className="transition-colors hover:bg-muted/60">
                      <td className="px-3 py-3">
                        <div className="inline-flex h-9 w-12 items-center justify-center rounded-md bg-primary text-sm font-semibold text-primary-foreground">
                          {item.score}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="font-medium text-foreground">{item.name}</div>
                        <div className="font-mono text-xs text-muted-foreground">{item.displayCode}</div>
                      </td>
                      <td className="px-3 py-3 text-muted-foreground">
                        {MARKET_OPTIONS.find((option) => option.value === item.market)?.label ?? item.market}
                      </td>
                      <td className="px-3 py-3">
                        <Badge variant={item.industry ? 'info' : 'default'}>{item.industry || '未分类'}</Badge>
                      </td>
                      <td className="px-3 py-3 text-right text-muted-foreground">
                        {formatNumber(item.price, { maximumFractionDigits: 3 })}
                      </td>
                      <td className={cn('px-3 py-3 text-right font-medium', getChangeClass(item.changePct))}>
                        {formatPct(item.changePct)}
                      </td>
                      <td className="px-3 py-3 text-right text-muted-foreground">{formatAmount(item.amount)}</td>
                      <td className="px-3 py-3 text-right text-muted-foreground">{formatAmount(item.volume)}</td>
                      <td className="px-3 py-3">
                        <Badge variant={QUOTE_STATUS_META[item.quoteStatus].variant}>
                          {QUOTE_STATUS_META[item.quoteStatus].label}
                        </Badge>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex max-w-[260px] flex-wrap gap-1.5">
                          {item.reasons.map((reason) => (
                            <Badge key={reason} variant={reason === '已自选' ? 'warning' : 'default'}>
                              {reason}
                            </Badge>
                          ))}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-1.5">
                          <WatchlistStarButton
                            stockName={item.name}
                            isStarred={watchlist.isWatchlisted(item.code, item.market)}
                            disabled={watchlist.disabled}
                            isSaving={watchlist.isSavingStock(item.code, item.market)}
                            onClick={() => void handleToggleWatchlist(item.code, item.name, item.market)}
                          />
                          <Tooltip content="查看 K 线">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="w-9 px-0"
                              onClick={() => handleOpenKLine(item.code, item.name)}
                              aria-label={`查看 ${item.name} K线`}
                            >
                              <ChartCandlestick className="h-4 w-4" />
                            </Button>
                          </Tooltip>
                          <Tooltip content="制定策略计划">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="w-9 px-0"
                              onClick={() => handleCreatePlan(item.code, item.name, item.market)}
                              aria-label={`为 ${item.name} 制定策略计划`}
                            >
                              <ListChecks className="h-4 w-4" />
                            </Button>
                          </Tooltip>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => void handleAnalyze(item.code, item.name)}
                            isLoading={analyzingCode === item.code}
                            loadingText="分析中"
                            aria-label={`分析 ${item.name}`}
                          >
                            <Play className="h-4 w-4" />
                            分析
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => handleAsk(item.code, item.name)}
                            aria-label={`问股 ${item.name}`}
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
            {totalPages > 1 ? (
              <div className="border-t border-border bg-muted/30 px-3 py-3">
                <Pagination
                  currentPage={clampedPage}
                  totalPages={totalPages}
                  onPageChange={setPage}
                  className="justify-end"
                />
              </div>
            ) : null}
          </div>
        ) : (
          <EmptyState
            title="没有候选股票"
            description="当前筛选条件下没有匹配的静态股票。"
            icon={<ShieldAlert className="h-6 w-6" />}
          />
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

const TOAST_VARIANT_STYLES: Record<NonNullable<ActionNotice>['variant'], string> = {
  success: 'border-success/30 bg-success/12 text-success shadow-success/10',
  warning: 'border-warning/35 bg-warning/12 text-warning shadow-warning/10',
  danger: 'border-danger/35 bg-danger/12 text-danger shadow-danger/10',
};

const FloatingActionToast: React.FC<{ notice: ActionNotice }> = ({ notice }) => {
  if (!notice) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed left-1/2 top-5 z-[95] w-[min(calc(100vw-2rem),34rem)] -translate-x-1/2 sm:left-auto sm:right-6 sm:translate-x-0"
    >
      <div className={cn('rounded-xl border px-4 py-3 shadow-2xl backdrop-blur-xl', TOAST_VARIANT_STYLES[notice.variant])}>
        <div className="text-sm font-semibold leading-5">{notice.title}</div>
        <div className="mt-1 text-sm leading-5 opacity-90">{notice.message}</div>
      </div>
    </div>
  );
};

export default CandidatePoolPage;
