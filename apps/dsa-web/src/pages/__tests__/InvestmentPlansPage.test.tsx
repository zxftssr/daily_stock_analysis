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
    create: vi.fn(),
    update: vi.fn(),
    setStatus: vi.fn(),
    setStepStatus: vi.fn(),
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
    vi.mocked(investmentPlansApi.create).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.setStatus).mockResolvedValue(plan);
    vi.mocked(investmentPlansApi.setStepStatus).mockResolvedValue(plan);
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

    fireEvent.click(screen.getByRole('button', { name: '完成' }));
    await waitFor(() => expect(investmentPlansApi.setStepStatus).toHaveBeenCalledWith(1, 11, 'completed'));

    fireEvent.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => expect(investmentPlansApi.setStepStatus).toHaveBeenCalledWith(1, 11, 'pending'));
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
    fireEvent.change(screen.getByLabelText('自动检查频率'), { target: { value: 'hourly' } });
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
        checkFrequency: 'hourly',
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
