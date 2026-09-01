import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { investmentPlansApi } from '../../api/investmentPlans';
import { portfolioApi } from '../../api/portfolio';
import type { InvestmentPlanItem } from '../../types/investmentPlan';
import InvestmentPlansPage from '../InvestmentPlansPage';

vi.mock('../../api/investmentPlans', () => ({
  investmentPlansApi: {
    list: vi.fn(),
    get: vi.fn(),
    getSchedulerStatus: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    setStatus: vi.fn(),
    setStepStatus: vi.fn(),
    recordStepExecution: vi.fn(),
    evaluate: vi.fn(),
    evaluateActive: vi.fn(),
  },
}));

vi.mock('../../api/portfolio', () => ({
  portfolioApi: {
    getAccounts: vi.fn(),
  },
}));

const plan: InvestmentPlanItem = {
  id: 1,
  accountId: null,
  symbol: '600519',
  market: 'cn',
  name: '贵州茅台',
  strategyType: 'value',
  strategyLabel: '价值投资',
  status: 'active',
  thesis: '行业龙头且现金流稳定',
  invalidationNote: '盈利能力持续恶化',
  benchmarkSymbol: '000300',
  maxPositionPct: 20,
  requiredCashPct: 25,
  reviewDate: '2026-12-31',
  notifyOnTrigger: true,
  notificationChannels: [],
  checkFrequency: 'daily',
  reviewDue: false,
  lastPrice: 1350,
  lastEvaluatedAt: '2026-08-24T18:00:00',
  lastEvaluationStatus: 'triggered',
  lastEvaluationNote: 'matched 1 pending step(s)',
  lastBlockedReasons: [],
  triggeredStepCount: 1,
  executionSummary: {
    completedExecutionCount: 0,
    unrecordedCompletedCount: 0,
    executionDataComplete: true,
    plannedCapital: null,
    totalQuantity: 0,
    grossAmount: 0,
    totalFees: 0,
    totalCost: 0,
    averageCost: null,
    remainingCash: null,
    valuationPrice: 1350,
    marketValue: 0,
    unrealizedPnl: null,
    returnPct: null,
    targetPositionPct: null,
    capitalUtilizationPct: null,
    targetDeviationPct: null,
  },
  steps: [{
    id: 11,
    planId: 1,
    action: 'buy',
    metric: 'price',
    operator: 'lte',
    threshold: 1400,
    upperThreshold: null,
    targetPositionPct: 5,
    note: '只使用第一笔闲置资金',
    sortOrder: 0,
    status: 'triggered',
    triggeredAt: '2026-08-24T18:00:00',
  }],
};

describe('InvestmentPlansPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(portfolioApi.getAccounts).mockResolvedValue({ accounts: [] });
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [],
      total: 0,
      summary: { active: 0, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.getSchedulerStatus).mockResolvedValue({
      status: 'not_started',
      online: false,
      message: '尚未检测到独立 schedule 服务',
      staleAfterSeconds: 90,
      heartbeatAgeSeconds: null,
      heartbeatAt: null,
      scheduleTime: null,
      minuteCheck: null,
    });
    vi.mocked(investmentPlansApi.create).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.get).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.setStatus).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.setStepStatus).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.recordStepExecution).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.evaluate).mockResolvedValue({
      plan,
      metricValues: { price: 1350 },
      matchedStepIds: [11],
      newlyTriggeredStepIds: [11],
      constraints: { positionPct: null, cashPct: null },
      blockedReasons: [],
      reviewDue: false,
      errors: [],
      notification: { attempted: false, sent: false, stepCount: 0 },
    });
    vi.mocked(investmentPlansApi.evaluateActive).mockResolvedValue({
      evaluated: 1,
      triggered: 1,
      errors: [],
      results: [],
      notification: { attempted: false, sent: false, stepCount: 1 },
    });
  });

  it('shows scheduler heartbeat and latest minute check status', async () => {
    vi.mocked(investmentPlansApi.getSchedulerStatus).mockResolvedValue({
      status: 'online',
      online: true,
      message: '独立 schedule 服务运行中',
      staleAfterSeconds: 90,
      heartbeatAgeSeconds: 3,
      heartbeatAt: '2026-08-31T09:31:03+08:00',
      scheduleTime: '18:00',
      minuteCheck: {
        status: 'completed',
        startedAt: '2026-08-31T09:31:00+08:00',
        completedAt: '2026-08-31T09:31:02+08:00',
        markets: ['cn'],
        evaluated: 2,
        triggered: 1,
        errorCount: 0,
        notificationSent: true,
        message: null,
      },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('调度服务在线')).toBeInTheDocument();
    expect(screen.getByText('独立 schedule 服务运行中')).toBeInTheDocument();
    expect(screen.getByText('检查完成')).toBeInTheDocument();
    expect(screen.getByText(/市场 CN · 检查 2 份 · 新触发 1 档 · 错误 0 项/)).toBeInTheDocument();
  });

  it('shows per-step notification failure and retry state', async () => {
    const failedPlan: InvestmentPlanItem = {
      ...plan,
      steps: [{
        ...plan.steps[0],
        notificationStatus: 'failed',
        notificationStatusAt: '2026-08-24T18:00:03',
        notificationError: '通知渠道返回发送失败',
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [failedPlan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    expect(await screen.findByText(/通知失败，待重试/)).toBeInTheDocument();
    expect(screen.getByText(/通知渠道返回发送失败/)).toBeInTheDocument();
  });

  it('renders the plan-first empty state and opens the editor', async () => {
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: '投资策略计划' })).toBeInTheDocument();
    expect(await screen.findByText('还没有策略计划')).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('button', { name: /制定计划/ })[0]);
    expect(screen.getByRole('dialog', { name: '制定策略计划' })).toBeInTheDocument();
    expect(screen.getAllByText('为什么投资').length).toBeGreaterThan(0);
    expect(screen.getByText('如何执行')).toBeInTheDocument();
    expect(screen.getByText('何时认错')).toBeInTheDocument();
  });

  it('hides removed plans by default while keeping them in all statuses', async () => {
    const removedPlan: InvestmentPlanItem = {
      ...plan,
      id: 2,
      name: '已移除价值计划',
      status: 'closed',
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan, removedPlan],
      total: 2,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('heading', { name: '贵州茅台' })).toBeInTheDocument();
    expect(screen.getByLabelText('状态')).toHaveValue('current');
    expect(screen.queryByRole('heading', { name: '已移除价值计划' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('状态'), { target: { value: '' } });

    expect(await screen.findByRole('heading', { name: '已移除价值计划' })).toBeInTheDocument();
  });

  it('prefills a new plan from route query parameters', async () => {
    render(
      <MemoryRouter initialEntries={['/plans?symbol=HK00700&name=腾讯控股&market=HK']}>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('dialog', { name: '制定策略计划' })).toBeInTheDocument();
    expect(screen.getByLabelText('标的代码')).toHaveValue('HK00700');
    expect(screen.getByLabelText('标的名称')).toHaveValue('腾讯控股');
    expect(screen.getByLabelText('市场')).toHaveValue('hk');
  });

  it('prefills the index crash template from ETF discovery', async () => {
    render(
      <MemoryRouter initialEntries={['/plans?symbol=510300&name=华泰柏瑞沪深300ETF&market=CN&strategyType=index_crash&benchmarkSymbol=510300']}>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('dialog', { name: '制定策略计划' })).toBeInTheDocument();
    expect(screen.getByLabelText('标的代码')).toHaveValue('510300');
    expect(screen.getByLabelText('标的名称')).toHaveValue('华泰柏瑞沪深300ETF');
    expect(screen.getByLabelText('策略类型')).toHaveValue('index_crash');
    expect(screen.getByLabelText('对标指数')).toHaveValue('510300');
    expect(screen.getAllByLabelText('检查指标')[0]).toHaveValue('benchmark_drawdown_250d_pct');
    expect(screen.getAllByLabelText('回撤阈值 %')[0]).toHaveValue(20);
  });

  it('applies strategy-specific scaffolding when a template is selected', async () => {
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('还没有策略计划');
    fireEvent.click(screen.getAllByRole('button', { name: /制定计划/ })[0]);

    fireEvent.change(screen.getByLabelText('策略类型'), { target: { value: 'index_crash' } });

    expect((screen.getByLabelText('为什么投资') as HTMLTextAreaElement).value).toContain('宽基指数');
    expect((screen.getByLabelText('什么情况下认错') as HTMLTextAreaElement).value).toContain('ETF');
    expect(screen.getByLabelText('对标指数')).toHaveValue('000300');
    expect(screen.getAllByLabelText('检查指标')[0]).toHaveValue('benchmark_drawdown_250d_pct');
    expect(screen.getAllByLabelText('回撤阈值 %')[0]).toHaveValue(20);
    expect(screen.getAllByText(/档位 0/)).toHaveLength(2);
  });

  it('renders the execution rail and handles evaluation and completion', async () => {
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.evaluate).mockResolvedValue({
      plan,
      metricValues: { price: 1350 },
      matchedStepIds: [11],
      newlyTriggeredStepIds: [11],
      constraints: { positionPct: null, cashPct: null },
      blockedReasons: [],
      reviewDue: false,
      errors: [],
      notification: { attempted: false, sent: false, queued: true, stepCount: 1 },
    });
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('执行轨道')).toBeInTheDocument();
    expect(screen.getByText('价格 ≤ 1,400')).toBeInTheDocument();
    expect(screen.getAllByText('已触发').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: '检查' }));
    await waitFor(() => expect(investmentPlansApi.evaluate).toHaveBeenCalledWith(1, true));
    expect(await screen.findByText('发现新触发条件')).toBeInTheDocument();
    expect(screen.getByText(/通知已进入后台发送/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '完成' }));
    await waitFor(() => expect(investmentPlansApi.setStepStatus).toHaveBeenCalledWith(1, 11, 'completed'));

    fireEvent.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => expect(investmentPlansApi.setStepStatus).toHaveBeenCalledWith(1, 11, 'pending'));
  });

  it('records a triggered ETF buy and renders the execution review', async () => {
    const etfPlan: InvestmentPlanItem = {
      ...plan,
      symbol: '510300',
      name: '华泰柏瑞沪深300ETF大跌分档计划',
      strategyType: 'index_crash',
      strategyLabel: '指数大跌',
      plannedCapital: 100000,
      lastPrice: 4,
      executionSummary: {
        ...plan.executionSummary,
        plannedCapital: 100000,
        remainingCash: 100000,
        valuationPrice: 4,
        marketValue: 0,
        capitalUtilizationPct: 0,
      },
      steps: [{
        ...plan.steps[0],
        metric: 'benchmark_drawdown_250d_pct',
        operator: 'gte',
        threshold: 5,
        targetPositionPct: 20,
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [etfPlan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.recordStepExecution).mockResolvedValue(etfPlan);

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    expect(await screen.findByText('执行复盘')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '登记买入' }));
    expect(screen.getByRole('dialog', { name: '登记实际买入' })).toBeInTheDocument();
    const executionAt = (screen.getByLabelText('实际成交时间') as HTMLInputElement).value;
    expect(executionAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{3})?)?$/);
    expect(screen.getByLabelText('成交价格')).toHaveValue(4);
    expect(screen.getByLabelText('成交份额')).toHaveValue(5000);
    fireEvent.change(screen.getByLabelText('手续费（CNY）'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('成交备注'), { target: { value: '券商回报' } });
    fireEvent.click(screen.getByRole('button', { name: '确认登记' }));

    await waitFor(() => expect(investmentPlansApi.recordStepExecution).toHaveBeenCalledWith(
      1,
      11,
      expect.objectContaining({ price: 4, quantity: 5000, fee: 5, note: '券商回报' }),
    ));
    const submittedExecutionAt = vi.mocked(investmentPlansApi.recordStepExecution).mock.calls[0][2].executionAt;
    expect(submittedExecutionAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
    expect(await screen.findByText('成交已登记')).toBeInTheDocument();
  });

  it('flags and backfills a legacy completed ETF tier without treating cash as complete', async () => {
    const legacyPlan: InvestmentPlanItem = {
      ...plan,
      symbol: '510300',
      strategyType: 'index_crash',
      strategyLabel: '指数大跌',
      status: 'closed',
      plannedCapital: null,
      lastPrice: 4,
      executionSummary: {
        ...plan.executionSummary,
        plannedCapital: null,
        unrecordedCompletedCount: 1,
        executionDataComplete: false,
        remainingCash: null,
        valuationPrice: 4,
        marketValue: null,
        capitalUtilizationPct: null,
      },
      steps: [{
        ...plan.steps[0],
        action: 'buy',
        metric: 'benchmark_drawdown_250d_pct',
        operator: 'gte',
        threshold: 5,
        targetPositionPct: 20,
        status: 'completed',
        executionAmount: null,
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [legacyPlan],
      total: 1,
      summary: { active: 0, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    fireEvent.change(screen.getByLabelText('状态'), { target: { value: 'closed' } });
    expect(await screen.findByText('旧版成交记录待补录')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '补录成交' }));
    fireEvent.change(screen.getByLabelText('成交份额'), { target: { value: '5000' } });
    fireEvent.click(screen.getByRole('button', { name: '确认登记' }));
    await waitFor(() => expect(investmentPlansApi.recordStepExecution).toHaveBeenCalledWith(
      1,
      11,
      expect.objectContaining({ price: 4, quantity: 5000 }),
    ));
  });

  it('suggests an unbound ETF fill from current plan equity and market value', async () => {
    const addPlan: InvestmentPlanItem = {
      ...plan,
      symbol: '510300',
      strategyType: 'index_crash',
      strategyLabel: '指数大跌',
      plannedCapital: 100000,
      lastPrice: 10,
      executionSummary: {
        ...plan.executionSummary,
        plannedCapital: 100000,
        executionDataComplete: true,
        totalQuantity: 1000,
        grossAmount: 20000,
        totalCost: 20000,
        remainingCash: 80000,
        valuationPrice: 10,
        marketValue: 10000,
        capitalUtilizationPct: 11.1111,
      },
      steps: [{
        ...plan.steps[0],
        action: 'add',
        metric: 'benchmark_drawdown_250d_pct',
        operator: 'gte',
        threshold: 10,
        targetPositionPct: 40,
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [addPlan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole('button', { name: '登记买入' }));
    expect(screen.getByText(/建议新增投入约 CNY 26,000/)).toBeInTheDocument();
    expect(screen.getByLabelText('成交份额')).toHaveValue(2600);
  });

  it('labels plan and execution amounts in each market local currency', async () => {
    const hkPlan: InvestmentPlanItem = {
      ...plan,
      id: 2,
      symbol: 'HK02800',
      market: 'hk',
      name: '盈富基金计划',
      strategyType: 'index_crash',
      strategyLabel: '指数大跌',
      plannedCapital: 1000,
      executionSummary: {
        ...plan.executionSummary,
        plannedCapital: 1000,
        completedExecutionCount: 1,
        totalCost: 200,
        remainingCash: 800,
        unrealizedPnl: 50,
      },
      steps: [{
        ...plan.steps[0],
        id: 21,
        planId: 2,
        status: 'completed',
        executionAmount: 200,
        executionFee: 2,
      }],
    };
    const usPlan: InvestmentPlanItem = {
      ...hkPlan,
      id: 3,
      symbol: 'SPY',
      market: 'us',
      name: 'SPY计划',
      plannedCapital: 2000,
      executionSummary: {
        ...hkPlan.executionSummary,
        plannedCapital: 2000,
        totalCost: 500,
        remainingCash: 1500,
        unrealizedPnl: -20,
      },
      steps: [{
        ...hkPlan.steps[0],
        id: 31,
        planId: 3,
        executionAmount: 500,
        executionFee: 1,
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [hkPlan, usPlan],
      total: 2,
      summary: { active: 2, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    expect(await screen.findByText('HKD（市场本币）')).toBeInTheDocument();
    expect(screen.getByText('USD（市场本币）')).toBeInTheDocument();
    expect(screen.getAllByText('HKD 1,000').length).toBeGreaterThan(0);
    expect(screen.getByText('HKD 800')).toBeInTheDocument();
    expect(screen.getByText('金额 HKD 200')).toBeInTheDocument();
    expect(screen.getByText('手续费 HKD 2')).toBeInTheDocument();
    expect(screen.getAllByText('USD 2,000').length).toBeGreaterThan(0);
    expect(screen.getByText('USD 1,500')).toBeInTheDocument();
    expect(screen.getByText('金额 USD 500')).toBeInTheDocument();
    expect(screen.getByText('手续费 USD 1')).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole('button', { name: '设置' })[0]);
    expect(screen.getAllByLabelText('计划资金').some((input) => input.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByText(/仍按 HKD 市场本币计价/)).toBeInTheDocument();
  });

  it('does not suggest a fill amount for an account-bound ETF plan', async () => {
    const boundPlan: InvestmentPlanItem = {
      ...plan,
      accountId: 9,
      symbol: '510300',
      market: 'us',
      strategyType: 'index_crash',
      strategyLabel: '指数大跌',
      plannedCapital: 100000,
      lastPrice: 10,
      executionSummary: {
        ...plan.executionSummary,
        plannedCapital: 100000,
        executionDataComplete: true,
        remainingCash: 100000,
        valuationPrice: 10,
        marketValue: 0,
        capitalUtilizationPct: 0,
      },
      steps: [{
        ...plan.steps[0],
        metric: 'benchmark_drawdown_250d_pct',
        operator: 'gte',
        threshold: 5,
        targetPositionPct: 20,
      }],
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [boundPlan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(<MemoryRouter><InvestmentPlansPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole('button', { name: '登记买入' }));
    expect(screen.getByText(/本页不自动建议金额/)).toBeInTheDocument();
    expect(screen.getByLabelText('手续费（USD）')).toBeInTheDocument();
    expect(screen.getByLabelText('成交份额')).toHaveValue(null);
  });

  it('reconciles a timed-out check when the backend already stored a newer result', async () => {
    const reconciledPlan = {
      ...plan,
      lastEvaluatedAt: '2026-08-27T19:26:17',
      lastEvaluationStatus: 'triggered',
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.evaluate).mockRejectedValue(Object.assign(
      new Error('timeout of 90000ms exceeded'),
      { code: 'ECONNABORTED' },
    ));
    vi.mocked(investmentPlansApi.get)
      .mockResolvedValueOnce(plan)
      .mockResolvedValueOnce(reconciledPlan);

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查' }));

    await waitFor(() => expect(investmentPlansApi.get).toHaveBeenCalledWith(1));
    expect(await screen.findByText('检查已在后台完成')).toBeInTheDocument();
    expect(screen.getByText(/已读取到新的检查结果：存在待处理触发/)).toBeInTheDocument();
  });

  it('does not treat an evaluation newer than a stale page snapshot as this timed-out check', async () => {
    const freshBaseline = {
      ...plan,
      lastEvaluatedAt: '2026-08-27T18:30:00',
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.evaluate).mockRejectedValue(Object.assign(
      new Error('timeout of 90000ms exceeded'),
      { code: 'ECONNABORTED' },
    ));
    vi.mocked(investmentPlansApi.get).mockResolvedValue(freshBaseline);

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查' }));

    await waitFor(() => expect(investmentPlansApi.get).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('检查已在后台完成')).not.toBeInTheDocument();
    expect(await screen.findByText(/后台可能仍在处理/)).toBeInTheDocument();
    const lockedCheck = screen.getByRole('button', { name: '重新确认' });
    expect(lockedCheck).toBeEnabled();
    expect(screen.getByRole('button', { name: '检查活跃计划' })).toBeDisabled();
    fireEvent.click(lockedCheck);
    expect(await screen.findByText('检查仍在后台处理中')).toBeInTheDocument();
    expect(investmentPlansApi.evaluate).toHaveBeenCalledTimes(1);
  });

  it('unlocks a timed-out check after the backend finishes later', async () => {
    const completedPlan = {
      ...plan,
      lastEvaluatedAt: '2026-08-27T19:30:00',
      lastEvaluationStatus: 'triggered',
    };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [completedPlan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.evaluate).mockRejectedValue(Object.assign(
      new Error('timeout of 90000ms exceeded'),
      { code: 'ECONNABORTED' },
    ));
    vi.mocked(investmentPlansApi.get)
      .mockResolvedValueOnce(plan)
      .mockResolvedValueOnce(plan)
      .mockResolvedValueOnce(completedPlan);

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查' }));

    const confirmButton = await screen.findByRole('button', { name: '重新确认' });
    fireEvent.click(confirmButton);

    expect(await screen.findByText('后台检查已完成')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检查' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '检查活跃计划' })).toBeEnabled();
    expect(investmentPlansApi.evaluate).toHaveBeenCalledTimes(1);
  });

  it('does not start an evaluation when the fresh baseline cannot be read', async () => {
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });
    vi.mocked(investmentPlansApi.get).mockRejectedValue(new Error('baseline unavailable'));

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查' }));

    expect(await screen.findByText('无法开始检查')).toBeInTheDocument();
    expect(investmentPlansApi.evaluate).not.toHaveBeenCalled();
  });

  it('warns when a single plan cannot validate conditions because data is missing', async () => {
    const missingPlan = { ...plan, lastEvaluationStatus: 'data_missing' };
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [missingPlan],
      total: 1,
      summary: { active: 1, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 1 },
    });
    vi.mocked(investmentPlansApi.evaluate).mockResolvedValue({
      plan: missingPlan,
      metricValues: { price: null },
      matchedStepIds: [],
      newlyTriggeredStepIds: [],
      constraints: { positionPct: null, cashPct: null },
      blockedReasons: [],
      reviewDue: false,
      errors: ['最新价格不可用'],
      notification: { attempted: false, sent: false, stepCount: 0 },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查' }));

    expect(await screen.findByText('行情数据不足')).toBeInTheDocument();
    expect(screen.getByText(/本次未完成条件校验：最新价格不可用/)).toBeInTheDocument();
  });

  it('creates and activates a plan with deterministic step fields', async () => {
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('还没有策略计划');
    fireEvent.click(screen.getAllByRole('button', { name: /制定计划/ })[0]);

    fireEvent.change(screen.getByLabelText('标的代码'), { target: { value: '600519' } });
    fireEvent.change(screen.getByLabelText('标的名称'), { target: { value: '贵州茅台' } });
    fireEvent.change(screen.getByLabelText('为什么投资'), { target: { value: '行业龙头且现金流稳定' } });
    fireEvent.change(screen.getByLabelText('什么情况下认错'), { target: { value: '盈利能力持续恶化' } });
    fireEvent.change(screen.getByLabelText('价格阈值'), { target: { value: '1400' } });
    fireEvent.change(screen.getByLabelText('执行后目标仓位 %'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('自动检查频率'), { target: { value: 'minute' } });
    fireEvent.change(screen.getByLabelText('通知渠道'), { target: { value: 'ntfy' } });
    fireEvent.click(screen.getByRole('button', { name: /保存并激活/ }));

    await waitFor(() => {
      expect(investmentPlansApi.create).toHaveBeenCalledWith(expect.objectContaining({
        symbol: '600519',
        market: 'cn',
        strategyType: 'value',
        status: 'active',
        maxPositionPct: 20,
        requiredCashPct: 25,
        notifyOnTrigger: true,
        notificationChannels: ['ntfy'],
        checkFrequency: 'minute',
        steps: [expect.objectContaining({
          action: 'buy',
          metric: 'price',
          operator: 'lte',
          threshold: 1400,
          targetPositionPct: 5,
        })],
      }));
    });
  });

  it('resets and hides minute checks for US plans', async () => {
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('还没有策略计划');
    fireEvent.click(screen.getAllByRole('button', { name: /制定计划/ })[0]);

    fireEvent.change(screen.getByLabelText('自动检查频率'), { target: { value: 'minute' } });
    fireEvent.change(screen.getByLabelText('市场'), { target: { value: 'us' } });

    expect(screen.getByLabelText('自动检查频率')).toHaveValue('daily');
    expect(screen.queryByRole('option', { name: '盘中高频（每分钟）' })).not.toBeInTheDocument();
    expect(screen.getByText(/美股需配置可靠的实时行情源后再开放/)).toBeInTheDocument();
  });

  it('checks every active plan without sending a user notification', async () => {
    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: '检查活跃计划' }));
    await waitFor(() => expect(investmentPlansApi.evaluateActive).toHaveBeenCalledWith(false));
    expect(await screen.findByText('活跃计划检查完成')).toBeInTheDocument();
  });

  it('reports an unknown batch outcome when both checking and refresh time out', async () => {
    vi.mocked(investmentPlansApi.list)
      .mockResolvedValueOnce({
        items: [plan],
        total: 1,
        summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
      })
      .mockRejectedValueOnce(Object.assign(new Error('network unavailable'), { code: 'ERR_NETWORK' }));
    vi.mocked(investmentPlansApi.evaluateActive).mockRejectedValue(Object.assign(
      new Error('timeout of 180000ms exceeded'),
      { code: 'ECONNABORTED' },
    ));

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    await screen.findByText('执行轨道');
    fireEvent.click(screen.getByRole('button', { name: '检查活跃计划' }));

    expect(await screen.findByText('批量检查状态未知')).toBeInTheDocument();
    expect(screen.getByText(/检查请求和状态刷新均未完成/)).toBeInTheDocument();
  });

  it('removes a plan while preserving the closed status contract', async () => {
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '移除' }));
    expect(screen.getByRole('alertdialog', { name: '移除策略计划' })).toHaveTextContent(
      '计划及执行历史仍会保留',
    );

    fireEvent.click(screen.getByRole('button', { name: '确认移除' }));

    await waitFor(() => expect(investmentPlansApi.setStatus).toHaveBeenCalledWith(1, 'closed'));
    expect(await screen.findByText('计划已移除')).toBeInTheDocument();
  });

  it('updates notification settings on an active plan without replacing steps', async () => {
    vi.mocked(investmentPlansApi.list).mockResolvedValue({
      items: [plan],
      total: 1,
      summary: { active: 1, triggered: 1, blocked: 0, reviewDue: 0, dataMissing: 0 },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole('button', { name: '设置' }));
    expect(screen.getByRole('dialog', { name: '检查与通知设置' })).toBeInTheDocument();
    expect(screen.getByText('执行条件保持不变')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('自动检查频率'), { target: { value: 'hourly' } });
    fireEvent.change(screen.getByLabelText('通知渠道'), { target: { value: 'ntfy' } });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => expect(investmentPlansApi.update).toHaveBeenCalledWith(1, {
      notifyOnTrigger: true,
      notificationChannels: ['ntfy'],
      checkFrequency: 'hourly',
    }));
    expect(investmentPlansApi.setStatus).not.toHaveBeenCalled();
    expect(await screen.findByText('检查与通知设置已保存')).toBeInTheDocument();
  });

  it('reports data-missing plans separately in batch checks', async () => {
    const missingPlan = { ...plan, lastEvaluationStatus: 'data_missing' };
    vi.mocked(investmentPlansApi.evaluateActive).mockResolvedValue({
      evaluated: 1,
      triggered: 0,
      errors: [],
      results: [{
        plan: missingPlan,
        metricValues: { price: null },
        matchedStepIds: [],
        newlyTriggeredStepIds: [],
        constraints: { positionPct: null, cashPct: null },
        blockedReasons: [],
        reviewDue: false,
        errors: ['最新价格不可用'],
        notification: { attempted: false, sent: false, stepCount: 0 },
      }],
      notification: { attempted: false, sent: false, stepCount: 0 },
    });

    render(
      <MemoryRouter>
        <InvestmentPlansPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: '检查活跃计划' }));

    expect(await screen.findByText('部分计划数据不足')).toBeInTheDocument();
    expect(screen.getByText(/数据不足 1 份/)).toBeInTheDocument();
  });
});
