import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backtestApi } from '../../api/backtest';
import { investmentPlansApi } from '../../api/investmentPlans';
import { useStockIndex } from '../../hooks/useStockIndex';
import { toDateInputValue } from '../../utils/format';
import EtfCrashBacktestPage from '../EtfCrashBacktestPage';

vi.mock('../../hooks/useStockIndex', () => ({ useStockIndex: vi.fn() }));
vi.mock('../../api/backtest', () => ({ backtestApi: { runEtfCrash: vi.fn() } }));
vi.mock('../../api/investmentPlans', () => ({ investmentPlansApi: { create: vi.fn() } }));

const result = {
  symbol: '510300',
  canonicalCode: '510300.SH',
  name: '沪深300ETF',
  benchmarkCode: '000300.SH',
  benchmarkName: '沪深300',
  source: 'sqlite',
  storageCode: '510300',
  requestedStartDate: '2025-08-26',
  requestedEndDate: '2026-08-26',
  effectiveStartDate: '2025-09-01',
  effectiveEndDate: '2026-08-26',
  tradingDays: 240,
  initialCapital: 100000,
  finalEquity: 108000,
  cashRemaining: 30000,
  positionValue: 78000,
  totalReturnPct: 8,
  buyHoldReturnPct: 12,
  excessReturnPct: -4,
  maxDrawdownPct: 9,
  capitalUtilizationPct: 35,
  maxPositionPct: 70,
  triggerCount: 2,
  triggeredStageCount: 2,
  untriggeredStageCount: 1,
  firstTriggerWaitTradingDays: 30,
  longestWaitTradingDays: 120,
  averageEntryPrice: 4.1,
  stages: [
    { drawdownPct: 10, targetPositionPct: 20 },
    { drawdownPct: 15, targetPositionPct: 40 },
    { drawdownPct: 20, targetPositionPct: 70 },
  ],
  trades: [{
    date: '2026-04-01',
    action: 'buy' as const,
    drawdownPct: 10.2,
    thresholdPct: 10,
    targetPositionPct: 20,
    price: 4,
    shares: 5000,
    cashAfter: 80000,
    positionPct: 20,
  }],
  equityCurve: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useStockIndex).mockReturnValue({
    index: [{
      canonicalCode: '510300.SH',
      displayCode: '510300',
      nameZh: '沪深300ETF',
      pinyinFull: '',
      pinyinAbbr: '',
      aliases: [],
      market: 'CN',
      assetType: 'etf',
      active: true,
      etfCategory: 'broad_market',
      benchmarkCode: '000300.SH',
      benchmarkName: '沪深300',
    }, {
      canonicalCode: '510500.SH',
      displayCode: '510500',
      nameZh: '中证500ETF',
      pinyinFull: '',
      pinyinAbbr: '',
      aliases: [],
      market: 'CN',
      assetType: 'etf',
      active: true,
      etfCategory: 'mid_cap',
      benchmarkCode: '000905.SH',
      benchmarkName: '中证500',
    }],
    loading: false,
    error: null,
    fallback: false,
    loaded: true,
  });
  vi.mocked(backtestApi.runEtfCrash).mockResolvedValue(result);
  vi.mocked(investmentPlansApi.create).mockResolvedValue({
    id: 88,
    symbol: '510300',
    market: 'cn',
    strategyType: 'index_crash',
    strategyLabel: '指数大跌',
    status: 'active',
    thesis: 'test',
    invalidationNote: 'test',
    notifyOnTrigger: true,
    notificationChannels: [],
    checkFrequency: 'daily',
    reviewDue: false,
    lastBlockedReasons: [],
    triggeredStepCount: 0,
    steps: [],
  });
});

describe('EtfCrashBacktestPage', () => {
  it('formats default dates from local calendar fields instead of UTC conversion', () => {
    const localDate = {
      getFullYear: () => 2026,
      getMonth: () => 7,
      getDate: () => 27,
    } as Date;

    expect(toDateInputValue(localDate)).toBe('2026-08-27');
  });

  it('runs a staged ETF drawdown backtest and renders core metrics', async () => {
    render(<MemoryRouter><EtfCrashBacktestPage /></MemoryRouter>);

    fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));

    await waitFor(() => expect(backtestApi.runEtfCrash).toHaveBeenCalledWith(expect.objectContaining({
      symbol: '510300',
      initialCapital: 100000,
      stages: [
        { drawdownPct: 10, targetPositionPct: 20 },
        { drawdownPct: 15, targetPositionPct: 40 },
        { drawdownPct: 20, targetPositionPct: 70 },
      ],
    })));
    expect(await screen.findByText('+8.00%')).toBeInTheDocument();
    expect(screen.getByText('2/3')).toBeInTheDocument();
    expect(screen.getByText('2026-04-01')).toBeInTheDocument();
  });

  it('creates an active account-free investment plan from the tested stages', async () => {
    render(<MemoryRouter><EtfCrashBacktestPage /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));
    fireEvent.click(await screen.findByRole('button', { name: '一键创建并启用策略' }));

    await waitFor(() => expect(investmentPlansApi.create).toHaveBeenCalledWith(expect.objectContaining({
      symbol: '510300',
      market: 'cn',
      strategyType: 'index_crash',
      status: 'active',
      benchmarkSymbol: '510300',
      maxPositionPct: 70,
      requiredCashPct: 30,
      notifyOnTrigger: true,
      checkFrequency: 'daily',
      steps: [
        expect.objectContaining({ action: 'buy', operator: 'gte', threshold: 10, targetPositionPct: 20 }),
        expect.objectContaining({ action: 'add', operator: 'gte', threshold: 15, targetPositionPct: 40 }),
        expect.objectContaining({ action: 'add', operator: 'gte', threshold: 20, targetPositionPct: 70 }),
      ],
    })));
    const payload = vi.mocked(investmentPlansApi.create).mock.calls[0][0];
    expect(payload.accountId).toBeUndefined();
    expect(await screen.findByText(/策略计划 #88 已创建并启用/)).toBeInTheDocument();
  });

  it('does not allow creating a plan from an old result after a failed rerun', async () => {
    vi.mocked(backtestApi.runEtfCrash)
      .mockResolvedValueOnce(result)
      .mockRejectedValueOnce(new Error('本地历史行情不足'));
    render(<MemoryRouter><EtfCrashBacktestPage /></MemoryRouter>);

    fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));
    expect(await screen.findByRole('button', { name: '一键创建并启用策略' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('第 1 档回撤'), { target: { value: '6' } });
    fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));

    await waitFor(() => expect(backtestApi.runEtfCrash).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument());
    expect(investmentPlansApi.create).not.toHaveBeenCalled();
  });

  it('invalidates a successful result whenever any strategy parameter changes', async () => {
    render(<MemoryRouter><EtfCrashBacktestPage /></MemoryRouter>);
    const run = async () => {
      fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));
      expect(await screen.findByRole('button', { name: '一键创建并启用策略' })).toBeInTheDocument();
    };

    await run();
    fireEvent.change(screen.getByLabelText('宽基 ETF'), { target: { value: '510500' } });
    expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument();

    await run();
    fireEvent.change(screen.getByLabelText('回测开始日期'), { target: { value: '2025-09-01' } });
    expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument();

    await run();
    fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '120000' } });
    expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument();

    await run();
    fireEvent.change(screen.getByLabelText('第 1 档回撤'), { target: { value: '6' } });
    expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument();
  });

  it('ignores an in-flight response after parameters change', async () => {
    let resolveRequest: ((value: typeof result) => void) | undefined;
    vi.mocked(backtestApi.runEtfCrash).mockReturnValueOnce(new Promise(resolve => {
      resolveRequest = resolve;
    }));
    render(<MemoryRouter><EtfCrashBacktestPage /></MemoryRouter>);

    fireEvent.click(screen.getByRole('button', { name: '运行 ETF 策略回测' }));
    fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '120000' } });
    resolveRequest?.(result);

    await waitFor(() => expect(backtestApi.runEtfCrash).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('button', { name: '一键创建并启用策略' })).not.toBeInTheDocument());
    expect(investmentPlansApi.create).not.toHaveBeenCalled();
  });
});
