import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  InvestmentPlanBatchEvaluationResponse,
  InvestmentPlanCreateRequest,
  InvestmentPlanEvaluationResponse,
  InvestmentPlanItem,
  InvestmentPlanListResponse,
  InvestmentPlanStatus,
  InvestmentPlanStepInput,
  InvestmentPlanStepStatus,
  InvestmentPlanUpdateRequest,
  InvestmentStrategyType,
} from '../types/investmentPlan';

type ListQuery = {
  status?: InvestmentPlanStatus;
  strategyType?: InvestmentStrategyType;
  symbol?: string;
  accountId?: number;
};

const serializeStep = (step: InvestmentPlanStepInput) => ({
  action: step.action,
  metric: step.metric,
  operator: step.operator,
  threshold: step.threshold,
  upper_threshold: step.upperThreshold ?? null,
  target_position_pct: step.targetPositionPct ?? null,
  note: step.note || null,
});

const serializePlan = (payload: InvestmentPlanCreateRequest | InvestmentPlanUpdateRequest) => ({
  ...('symbol' in payload ? { symbol: payload.symbol } : {}),
  ...('market' in payload ? { market: payload.market } : {}),
  ...('name' in payload ? { name: payload.name || null } : {}),
  ...('accountId' in payload ? { account_id: payload.accountId ?? null } : {}),
  ...('strategyType' in payload ? { strategy_type: payload.strategyType } : {}),
  ...('status' in payload ? { status: payload.status } : {}),
  ...('thesis' in payload ? { thesis: payload.thesis } : {}),
  ...('invalidationNote' in payload ? { invalidation_note: payload.invalidationNote } : {}),
  ...('benchmarkSymbol' in payload ? { benchmark_symbol: payload.benchmarkSymbol || null } : {}),
  ...('maxPositionPct' in payload ? { max_position_pct: payload.maxPositionPct ?? null } : {}),
  ...('requiredCashPct' in payload ? { required_cash_pct: payload.requiredCashPct ?? null } : {}),
  ...('reviewDate' in payload ? { review_date: payload.reviewDate || null } : {}),
  ...('steps' in payload && payload.steps ? { steps: payload.steps.map(serializeStep) } : {}),
});

export const investmentPlansApi = {
  async list(query: ListQuery = {}): Promise<InvestmentPlanListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/investment-plans', {
      params: {
        status: query.status,
        strategy_type: query.strategyType,
        symbol: query.symbol,
        account_id: query.accountId,
      },
    });
    return toCamelCase<InvestmentPlanListResponse>(response.data);
  },

  async get(planId: number): Promise<InvestmentPlanItem> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/investment-plans/${planId}`);
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async create(payload: InvestmentPlanCreateRequest): Promise<InvestmentPlanItem> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/investment-plans',
      serializePlan(payload),
    );
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async update(planId: number, payload: InvestmentPlanUpdateRequest): Promise<InvestmentPlanItem> {
    const response = await apiClient.put<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}`,
      serializePlan(payload),
    );
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async setStatus(planId: number, status: InvestmentPlanStatus): Promise<InvestmentPlanItem> {
    const response = await apiClient.patch<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}/status`,
      { status },
    );
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async setStepStatus(
    planId: number,
    stepId: number,
    status: InvestmentPlanStepStatus,
  ): Promise<InvestmentPlanItem> {
    const response = await apiClient.patch<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}/steps/${stepId}`,
      { status },
    );
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async evaluate(planId: number): Promise<InvestmentPlanEvaluationResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}/evaluate`,
    );
    return toCamelCase<InvestmentPlanEvaluationResponse>(response.data);
  },

  async evaluateActive(notify = false): Promise<InvestmentPlanBatchEvaluationResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/investment-plans/evaluate-active',
      undefined,
      { params: { notify } },
    );
    return toCamelCase<InvestmentPlanBatchEvaluationResponse>(response.data);
  },
};
