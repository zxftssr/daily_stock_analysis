import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DiscoverPage from '../DiscoverPage';
import { analysisApi, DuplicateTaskError } from '../../api/analysis';
import { stocksApi } from '../../api/stocks';
import { systemConfigApi, SystemConfigConflictError } from '../../api/systemConfig';
import { useStockIndex } from '../../hooks/useStockIndex';
import type { StockIndexItem } from '../../types/stockIndex';
import type { SystemConfigResponse, UpdateSystemConfigResponse } from '../../types/systemConfig';

vi.mock('../../hooks/useStockIndex', () => ({
  useStockIndex: vi.fn(),
}));

vi.mock('../../api/stocks', () => ({
  stocksApi: {
    getRankings: vi.fn(),
  },
}));

vi.mock('../../api/analysis', () => {
  class MockDuplicateTaskError extends Error {
    stockCode: string;
    existingTaskId: string;

    constructor(stockCode: string, existingTaskId: string, message?: string) {
      super(message || `股票 ${stockCode} 正在分析中`);
      this.name = 'DuplicateTaskError';
      this.stockCode = stockCode;
      this.existingTaskId = existingTaskId;
    }
  }

  return {
    analysisApi: {
      analyzeAsync: vi.fn(),
    },
    DuplicateTaskError: MockDuplicateTaskError,
  };
});

vi.mock('../../api/systemConfig', () => {
  class MockSystemConfigConflictError extends Error {
    currentConfigVersion?: string;

    constructor(message: string, currentConfigVersion?: string) {
      super(message);
      this.name = 'SystemConfigConflictError';
      this.currentConfigVersion = currentConfigVersion;
    }
  }

  return {
    systemConfigApi: {
      getConfig: vi.fn(),
      update: vi.fn(),
    },
    SystemConfigConflictError: MockSystemConfigConflictError,
  };
});

vi.mock('../../components/stocks/StockKLineDrawer', () => ({
  StockKLineDrawer: ({
    isOpen,
    stockCode,
    stockName,
  }: {
    isOpen: boolean;
    stockCode?: string;
    stockName?: string;
  }) => (isOpen ? <div data-testid="kline-drawer">{stockName} {stockCode}</div> : null),
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

const index: StockIndexItem[] = [
  {
    canonicalCode: '600519.SH',
    displayCode: '600519',
    nameZh: '贵州茅台',
    pinyinFull: 'guizhoumaotai',
    pinyinAbbr: 'gzmt',
    aliases: ['茅台'],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 100,
    industry: '白酒',
    industrySource: 'override',
  },
  {
    canonicalCode: '000001.SZ',
    displayCode: '000001',
    nameZh: '平安银行',
    pinyinFull: 'pinganyinhang',
    pinyinAbbr: 'payh',
    aliases: ['平银'],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 90,
    industry: '银行',
    industrySource: 'tushare',
  },
  {
    canonicalCode: '000002.SZ',
    displayCode: '000002',
    nameZh: '万科A',
    pinyinFull: 'wankea',
    pinyinAbbr: 'wka',
    aliases: [],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 80,
  },
  {
    canonicalCode: '00700.HK',
    displayCode: '00700',
    nameZh: '腾讯控股',
    pinyinFull: 'tengxunkonggu',
    pinyinAbbr: 'txkg',
    aliases: ['腾讯'],
    market: 'HK',
    assetType: 'stock',
    active: true,
    popularity: 95,
    industry: '互联网服务',
    industrySource: 'override',
  },
  {
    canonicalCode: '510300.SH',
    displayCode: '510300',
    nameZh: '华泰柏瑞沪深300ETF',
    pinyinFull: 'huataibairuihushen300ETF',
    pinyinAbbr: 'htbrhs300ETF',
    aliases: ['300ETF'],
    market: 'CN',
    assetType: 'etf',
    active: true,
    popularity: 80,
    etfCategory: 'broad_market',
    benchmarkCode: '000300.SH',
    benchmarkName: '沪深300',
  },
];

const createDiscoverStock = (idx: number): StockIndexItem => {
  const code = String(100000 + idx).padStart(6, '0');
  return {
    canonicalCode: `${code}.SZ`,
    displayCode: code,
    nameZh: `测试股票${idx}`,
    pinyinFull: `ceshigupiao${idx}`,
    pinyinAbbr: `csgp${idx}`,
    aliases: [],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 1,
    industry: '测试行业',
    industrySource: 'override',
  };
};

const createConfigResponse = (
  stockList: string,
  overrides: Partial<SystemConfigResponse> = {},
): SystemConfigResponse => ({
  configVersion: 'config-v1',
  maskToken: 'mask-1',
  items: [
    {
      key: 'STOCK_LIST',
      value: stockList,
      rawValueExists: true,
      isMasked: false,
    },
  ],
  ...overrides,
});

const createUpdateResponse = (
  overrides: Partial<UpdateSystemConfigResponse> = {},
): UpdateSystemConfigResponse => ({
  success: true,
  configVersion: 'config-v2',
  appliedCount: 1,
  skippedMaskedCount: 0,
  reloadTriggered: true,
  updatedKeys: ['STOCK_LIST'],
  warnings: [],
  ...overrides,
});

describe('DiscoverPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useStockIndex).mockReturnValue({
      index,
      loading: false,
      error: null,
      fallback: false,
      loaded: true,
    });
    vi.mocked(stocksApi.getRankings).mockResolvedValue({
      status: 'ok',
      source: 'mock',
      updatedAt: '2026-06-21T00:00:00+00:00',
      items: [],
    });
    vi.mocked(analysisApi.analyzeAsync).mockResolvedValue({
      taskId: 'task-1',
      status: 'pending',
    });
    vi.mocked(systemConfigApi.getConfig).mockResolvedValue(createConfigResponse('600519,HK00700'));
    vi.mocked(systemConfigApi.update).mockResolvedValue(createUpdateResponse());
  });

  it('filters by keyword and calculates industry coverage after keyword filtering', async () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('行业覆盖 2/3')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('关键词'), { target: { value: '平安' } });

    expect(screen.getByText('平安银行')).toBeInTheDocument();
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument();
    expect(screen.getByText('行业覆盖 1/1')).toBeInTheDocument();

    await waitFor(() => {
      expect(stocksApi.getRankings).toHaveBeenCalledWith(expect.objectContaining({
        market: 'CN',
        metric: 'change_pct',
        direction: 'desc',
      }));
    });
  });

  it('renders discovery filters and metrics in a compact toolbar', () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('discover-compact-toolbar')).toBeInTheDocument();
    expect(screen.getByTestId('discover-page')).toHaveClass('max-w-[2160px]');
    expect(screen.getByTestId('discover-search-field')).toHaveClass('2xl:max-w-[720px]');
    expect(screen.getByTestId('discover-filter-grid')).toHaveClass(
      '2xl:grid-cols-[160px_minmax(260px,720px)_220px_minmax(360px,1fr)]',
    );
    expect(screen.getByTestId('discover-compact-toolbar')).toHaveAttribute('data-slot', 'toolbar');
    expect(screen.getByTestId('discover-compact-metrics')).toHaveTextContent('当前市场');
    expect(screen.getByTestId('discover-compact-metrics')).toHaveTextContent('当前结果');
    expect(screen.getByTestId('discover-compact-metrics')).toHaveTextContent('行业覆盖率');
    expect(screen.getByTestId('discover-stock-table-scroll')).toHaveAttribute('data-slot', 'data-table');
  });

  it('switches to broad ETF discovery and opens a prefilled crash plan', async () => {
    vi.mocked(stocksApi.getRankings).mockImplementation(async (params) => ({
      status: 'ok',
      source: 'mock-etf',
      updatedAt: '2026-08-26T00:00:00+00:00',
      items: params.assetType === 'etf' ? [{
        code: '510300.SH',
        name: '华泰柏瑞沪深300ETF',
        market: 'CN',
        assetType: 'etf',
        category: 'broad_market',
        benchmarkCode: '000300.SH',
        benchmarkName: '沪深300',
        price: 4.2,
        changePct: -1.2,
        amount: 2_000_000_000,
        volume: 100_000_000,
        drawdown250dPct: 18.5,
        return20dPct: -6.2,
        return60dPct: -10.1,
        return250dPct: -8.4,
      }] : [],
    }));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: '宽基 ETF' }));

    await waitFor(() => {
      expect(stocksApi.getRankings).toHaveBeenCalledWith(expect.objectContaining({
        market: 'CN',
        assetType: 'etf',
        metric: 'drawdown_250d_pct',
        direction: 'desc',
      }));
    });
    expect(screen.getByRole('heading', { name: '宽基 ETF 精选池' })).toBeInTheDocument();
    expect(screen.getAllByText('沪深300').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '成交量' }));
    await waitFor(() => {
      expect(stocksApi.getRankings).toHaveBeenCalledWith(expect.objectContaining({
        assetType: 'etf',
        metric: 'volume',
        direction: 'desc',
      }));
    });
    expect(screen.getAllByText('成交量')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '为 华泰柏瑞沪深300ETF 制定计划' }));
    expect(mockNavigate).toHaveBeenCalledWith(
      '/plans?symbol=510300&name=%E5%8D%8E%E6%B3%B0%E6%9F%8F%E7%91%9E%E6%B2%AA%E6%B7%B1300ETF&market=CN&strategyType=index_crash&benchmarkSymbol=510300',
    );
  });

  it('distinguishes unavailable rankings from empty ranking results', async () => {
    vi.mocked(stocksApi.getRankings).mockResolvedValueOnce({
      status: 'unavailable',
      source: null,
      updatedAt: null,
      message: '批量行情源暂不可用，且没有可用缓存',
      items: [],
    });

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('行情源不可用')).toBeInTheDocument();
    expect(screen.getByText('批量行情源暂不可用，且没有可用缓存')).toBeInTheDocument();
  });

  it('submits analysis with discover selection source and shows duplicate task feedback', async () => {
    vi.mocked(analysisApi.analyzeAsync).mockRejectedValueOnce(
      new DuplicateTaskError('600519.SH', 'task-1', '股票 600519.SH 正在分析中'),
    );

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getAllByRole('button', { name: /分析/ })[0]);

    await waitFor(() => {
      expect(analysisApi.analyzeAsync).toHaveBeenCalledWith(expect.objectContaining({
        stockCode: '600519.SH',
        stockName: '贵州茅台',
        selectionSource: 'discover',
        asyncMode: true,
      }));
    });
    expect(await screen.findByText('分析已在进行')).toBeInTheDocument();
  });

  it('opens K-line drawer from the stock table action', async () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看 贵州茅台 K线' }));

    expect(screen.getByTestId('kline-drawer')).toHaveTextContent('贵州茅台 600519.SH');
  });

  it('opens K-line drawer from a ranking tile', async () => {
    vi.mocked(stocksApi.getRankings).mockResolvedValueOnce({
      status: 'ok',
      source: 'mock',
      updatedAt: '2026-06-21T00:00:00+00:00',
      items: [
        {
          code: '00700.HK',
          name: '腾讯控股',
          market: 'HK',
          industry: '互联网服务',
          price: 400,
          changePct: 3.1,
          amount: 120000000,
          volume: 1000000,
        },
      ],
    });

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '查看 腾讯控股 K线' }));

    expect(screen.getByTestId('kline-drawer')).toHaveTextContent('腾讯控股 00700.HK');
  });

  it('loads watchlist config and renders solid or empty stars', async () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(systemConfigApi.getConfig).toHaveBeenCalledWith(false);
    });
    expect(await screen.findByRole('button', { name: '从自选移除 贵州茅台' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '加入自选 平安银行' })).toBeInTheDocument();
  });

  it('appends a standard watchlist code and updates the local config version after save', async () => {
    vi.mocked(systemConfigApi.getConfig).mockResolvedValueOnce(createConfigResponse('600519,HK00700'));
    vi.mocked(systemConfigApi.update)
      .mockResolvedValueOnce(createUpdateResponse({ configVersion: 'config-v2' }))
      .mockResolvedValueOnce(createUpdateResponse({ configVersion: 'config-v3' }));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '加入自选 平安银行' }));

    await waitFor(() => {
      expect(systemConfigApi.update).toHaveBeenCalledWith(expect.objectContaining({
        configVersion: 'config-v1',
        maskToken: 'mask-1',
        items: [{ key: 'STOCK_LIST', value: '600519,HK00700,000001' }],
      }));
    });
    expect(await screen.findByRole('button', { name: '从自选移除 平安银行' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '加入自选 万科A' }));

    await waitFor(() => {
      expect(systemConfigApi.update).toHaveBeenLastCalledWith(expect.objectContaining({
        configVersion: 'config-v2',
        maskToken: 'mask-1',
        items: [{ key: 'STOCK_LIST', value: '600519,HK00700,000001,000002' }],
      }));
    });
    expect(systemConfigApi.getConfig).toHaveBeenCalledTimes(1);
  });

  it('removes all equivalent watchlist codes when unstarred', async () => {
    vi.mocked(systemConfigApi.getConfig).mockResolvedValueOnce(createConfigResponse('600519,600519.SH,HK00700'));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '从自选移除 贵州茅台' }));

    await waitFor(() => {
      expect(systemConfigApi.update).toHaveBeenCalledWith(expect.objectContaining({
        items: [{ key: 'STOCK_LIST', value: 'HK00700' }],
      }));
    });
  });

  it('renders action feedback as a floating toast without taking page layout space', async () => {
    vi.mocked(systemConfigApi.getConfig).mockResolvedValueOnce(createConfigResponse('600519,HK00700'));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '从自选移除 贵州茅台' }));

    const toast = await screen.findByTestId('discover-action-toast');
    expect(toast).toHaveTextContent('已移出自选');
    expect(toast).toHaveClass('fixed');
  });

  it('disables all star buttons while a watchlist save is running', async () => {
    let resolveUpdate: (value: UpdateSystemConfigResponse) => void = () => {};
    vi.mocked(systemConfigApi.getConfig).mockResolvedValueOnce(createConfigResponse('600519,HK00700'));
    vi.mocked(systemConfigApi.update).mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '加入自选 平安银行' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '加入自选 万科A' })).toBeDisabled();
    });

    resolveUpdate(createUpdateResponse());
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '加入自选 万科A' })).not.toBeDisabled();
    });
  });

  it('reloads config on version conflict without silently applying the clicked change', async () => {
    vi.mocked(systemConfigApi.getConfig)
      .mockResolvedValueOnce(createConfigResponse('600519'))
      .mockResolvedValueOnce(createConfigResponse('600519,000002', { configVersion: 'config-v2', maskToken: 'mask-2' }));
    vi.mocked(systemConfigApi.update).mockRejectedValueOnce(
      new SystemConfigConflictError('配置版本冲突', 'config-v2'),
    );

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '加入自选 平安银行' }));

    expect(await screen.findByText('自选配置已更新')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '加入自选 平安银行' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '从自选移除 万科A' })).toBeInTheDocument();
  });

  it('keeps the last watchlist and lets users exit watchlist-only after a conflict reload fails', async () => {
    const largeIndex = Array.from({ length: 125 }, (_, idx) => createDiscoverStock(idx + 1));
    vi.mocked(useStockIndex).mockReturnValue({
      index: largeIndex,
      loading: false,
      error: null,
      fallback: false,
      loaded: true,
    });
    vi.mocked(systemConfigApi.getConfig)
      .mockResolvedValueOnce(createConfigResponse('100061'))
      .mockRejectedValueOnce(new Error('刷新失败'));
    vi.mocked(systemConfigApi.update).mockRejectedValueOnce(
      new SystemConfigConflictError('配置版本冲突', 'config-v2'),
    );

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '只看自选' }));
    expect(screen.getByText('显示 1-1 / 1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '从自选移除 测试股票61' }));

    expect(await screen.findByText('自选配置不可用')).toBeInTheDocument();
    expect(screen.getByText('测试股票61')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '只看自选' })).not.toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '只看自选' }));

    expect(screen.getByText('显示 1-50 / 125')).toBeInTheDocument();
  });

  it('standardizes existing bare HK codes using the stock index on later saves', async () => {
    vi.mocked(systemConfigApi.getConfig).mockResolvedValueOnce(createConfigResponse('00700'));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '加入自选 平安银行' }));

    await waitFor(() => {
      expect(systemConfigApi.update).toHaveBeenCalledWith(expect.objectContaining({
        items: [{ key: 'STOCK_LIST', value: 'HK00700,000001' }],
      }));
    });
  });

  it('keeps discovery usable but disables stars when config loading fails', async () => {
    vi.mocked(systemConfigApi.getConfig).mockRejectedValueOnce(new Error('配置服务不可用'));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('自选配置不可用')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '加入自选 平安银行' })).toBeDisabled();
    expect(screen.getAllByRole('button', { name: /分析/ })[0]).not.toBeDisabled();
  });

  it('filters to watchlist only and resets the stock page', async () => {
    const largeIndex = Array.from({ length: 125 }, (_, idx) => createDiscoverStock(idx + 1));
    vi.mocked(useStockIndex).mockReturnValue({
      index: largeIndex,
      loading: false,
      error: null,
      fallback: false,
      loaded: true,
    });
    vi.mocked(systemConfigApi.getConfig).mockResolvedValue(createConfigResponse('100061'));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '2' }));
    expect(screen.getByText('显示 51-100 / 125')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: '只看自选' }));

    expect(screen.getByText('显示 1-1 / 1')).toBeInTheDocument();
    expect(screen.getByText('测试股票61')).toBeInTheDocument();
  });

  it('uses the same watchlist helper for ranking star toggles', async () => {
    vi.mocked(stocksApi.getRankings).mockResolvedValueOnce({
      status: 'ok',
      source: 'mock',
      updatedAt: '2026-06-21T00:00:00+00:00',
      items: [
        {
          code: '00700.HK',
          name: '腾讯控股',
          market: 'HK',
          industry: '互联网服务',
          price: 400,
          changePct: 3.1,
          amount: 120000000,
          volume: 1000000,
        },
      ],
    });
    vi.mocked(systemConfigApi.getConfig)
      .mockResolvedValueOnce(createConfigResponse(''))
      .mockResolvedValueOnce(createConfigResponse('HK00700', { configVersion: 'config-v2', maskToken: 'mask-2' }));

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '加入自选 腾讯控股' }));

    await waitFor(() => {
      expect(systemConfigApi.update).toHaveBeenCalledWith(expect.objectContaining({
        items: [{ key: 'STOCK_LIST', value: 'HK00700' }],
      }));
    });
  });

  it('paginates the discoverable stock list with compact page sizes', async () => {
    const largeIndex = Array.from({ length: 125 }, (_, idx) => createDiscoverStock(idx + 1));
    vi.mocked(useStockIndex).mockReturnValue({
      index: largeIndex,
      loading: false,
      error: null,
      fallback: false,
      loaded: true,
    });

    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('显示 1-50 / 125')).toBeInTheDocument();
    expect(screen.getByText('测试股票1')).toBeInTheDocument();
    expect(screen.queryByText('测试股票51')).not.toBeInTheDocument();
    expect(screen.getByTestId('discover-stock-table-scroll')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '2' }));

    expect(screen.getByText('显示 51-100 / 125')).toBeInTheDocument();
    expect(screen.queryByText('测试股票1')).not.toBeInTheDocument();
    expect(screen.getByText('测试股票51')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('每页'), { target: { value: '100' } });

    expect(screen.getByText('显示 1-100 / 125')).toBeInTheDocument();
    expect(screen.getByText('测试股票1')).toBeInTheDocument();
    expect(screen.queryByText('测试股票101')).not.toBeInTheDocument();
  });
});
