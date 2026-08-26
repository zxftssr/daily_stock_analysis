import apiClient from './index';
import { toCamelCase } from './utils';
import type { Market } from '../types/stockIndex';

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

export type RankingMetric = 'change_pct' | 'amount' | 'volume' | 'drawdown_250d_pct'
  | 'return_20d_pct' | 'return_60d_pct' | 'return_250d_pct';
export type RankingDirection = 'asc' | 'desc';
export type RankingStatus = 'ok' | 'partial' | 'stale' | 'unsupported' | 'unavailable';

export type StockRankingItem = {
  code: string;
  name: string;
  market: Market;
  industry?: string | null;
  price?: number | null;
  changePct?: number | null;
  amount?: number | null;
  volume?: number | null;
  source?: string | null;
  updatedAt?: string | null;
  assetType?: 'stock' | 'etf';
  category?: string | null;
  benchmarkCode?: string | null;
  benchmarkName?: string | null;
  drawdown250dPct?: number | null;
  return20dPct?: number | null;
  return60dPct?: number | null;
  return250dPct?: number | null;
  historyAsOfDate?: string | null;
  historyStale?: boolean | null;
};

export type StockRankingsResponse = {
  status: RankingStatus;
  source?: string | null;
  updatedAt?: string | null;
  message?: string | null;
  historyAsOfDate?: string | null;
  historyCoverage?: number | null;
  historyTotal?: number | null;
  historyStale?: boolean | null;
  items: StockRankingItem[];
};

export type EtfHistoryWarmupItem = {
  code: string;
  name: string;
  status: 'ok' | 'stale' | 'unavailable' | 'error' | 'timeout';
  source?: string | null;
  asOfDate?: string | null;
  actualRecords?: number | null;
  drawdown250dPct?: number | null;
  message?: string | null;
};

export type EtfHistoryWarmupResponse = {
  status: 'ok' | 'partial' | 'unavailable';
  startedAt: string;
  completedAt: string;
  total: number;
  succeeded: number;
  stale: number;
  failed: number;
  items: EtfHistoryWarmupItem[];
};

export type StockRankingsParams = {
  market: Extract<Market, 'CN' | 'BSE' | 'HK' | 'US'>;
  industry?: string;
  metric?: RankingMetric;
  direction?: RankingDirection;
  limit?: number;
  assetType?: 'stock' | 'etf';
  category?: string;
};

export type KLineData = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  amount?: number | null;
  changePercent?: number | null;
};

export type StockHistoryResponse = {
  stockCode: string;
  stockName?: string | null;
  period: 'daily';
  source?: string | null;
  cacheHit?: boolean | null;
  stale?: boolean | null;
  partialCache?: boolean | null;
  asOfDate?: string | null;
  actualRecords?: number | null;
  requestedDays?: number | null;
  effectiveDays?: number | null;
  message?: string | null;
  data: KLineData[];
};

export type StockHistoryDays = 30 | 90 | 180 | 365;

export type StockHistoryParams = {
  days?: StockHistoryDays;
  forceRefresh?: boolean;
  signal?: AbortSignal;
};

export const stocksApi = {
  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },

  async getRankings(params: StockRankingsParams, signal?: AbortSignal): Promise<StockRankingsResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/stocks/rankings',
      {
        params: {
          market: params.market,
          industry: params.industry,
          metric: params.metric,
          direction: params.direction,
          limit: params.limit,
          asset_type: params.assetType,
          category: params.category,
        },
        signal,
      }
    );
    return toCamelCase<StockRankingsResponse>(response.data);
  },

  async warmEtfHistory(forceRefresh = true): Promise<EtfHistoryWarmupResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/stocks/etf-history/warmup',
      undefined,
      {
        params: { force_refresh: forceRefresh },
        timeout: 130000,
      },
    );
    return toCamelCase<EtfHistoryWarmupResponse>(response.data);
  },

  async getHistory(stockCode: string, params: StockHistoryParams = {}): Promise<StockHistoryResponse> {
    const { days = 90, forceRefresh = false, signal } = params;
    const encodedStockCode = encodeURIComponent(stockCode);
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodedStockCode}/history`,
      {
        params: {
          period: 'daily',
          days,
          force_refresh: forceRefresh,
        },
        signal,
      },
    );
    return toCamelCase<StockHistoryResponse>(response.data);
  },
};
