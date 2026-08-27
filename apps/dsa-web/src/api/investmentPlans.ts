import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  InvestmentPlanBatchEvaluationResponse,
  InvestmentPlanCreateRequest,
  InvestmentPlanEvaluationResponse,
  InvestmentPlanExecutionRequest,
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

const PLAN_EVALUATION_TIMEOUT_MS = 90_000;
const PLAN_BATCH_EVALUATION_TIMEOUT_MS = 180_000;

export const serializeExecutionAt = (
  value: string,
  timezoneOffsetMinutes = new Date(value).getTimezoneOffset(),
) => {
  const normalizedValue = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)
    ? `${value}:00`
    : value;
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(normalizedValue)) {
    throw new Error('executionAt must include local date and time to seconds');
  }
  if (!Number.isFinite(timezoneOffsetMinutes)) {
    throw new Error('Browser timezone offset is unavailable');
  }
  const sign = timezoneOffsetMinutes <= 0 ? '+' : '-';
  const absoluteMinutes = Math.abs(timezoneOffsetMinutes);
  const hours = String(Math.floor(absoluteMinutes / 60)).padStart(2, '0');
  const minutes = String(absoluteMinutes % 60).padStart(2, '0');
  return `${normalizedValue}${sign}${hours}:${minutes}`;
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
  ...('plannedCapital' in payload ? { planned_capital: payload.plannedCapital ?? null } : {}),
  ...('maxPositionPct' in payload ? { max_position_pct: payload.maxPositionPct ?? null } : {}),
  ...('requiredCashPct' in payload ? { required_cash_pct: payload.requiredCashPct ?? null } : {}),
  ...('reviewDate' in payload ? { review_date: payload.reviewDate || null } : {}),
  ...('notifyOnTrigger' in payload ? { notify_on_trigger: payload.notifyOnTrigger } : {}),
  ...('notificationChannels' in payload ? { notification_channels: payload.notificationChannels || [] } : {}),
  ...('checkFrequency' in payload ? { check_frequency: payload.checkFrequency } : {}),
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

  async recordStepExecution(
    planId: number,
    stepId: number,
    payload: InvestmentPlanExecutionRequest,
  ): Promise<InvestmentPlanItem> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}/steps/${stepId}/execution`,
      {
        execution_at: serializeExecutionAt(payload.executionAt),
        price: payload.price,
        quantity: payload.quantity,
        fee: payload.fee ?? 0,
        note: payload.note || null,
      },
    );
    return toCamelCase<InvestmentPlanItem>(response.data);
  },

  async evaluate(planId: number, notify = false): Promise<InvestmentPlanEvaluationResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/investment-plans/${planId}/evaluate`,
      undefined,
      { params: { notify }, timeout: PLAN_EVALUATION_TIMEOUT_MS },
    );
    return toCamelCase<InvestmentPlanEvaluationResponse>(response.data);
  },

  async evaluateActive(notify = false): Promise<InvestmentPlanBatchEvaluationResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/investment-plans/evaluate-active',
      undefined,
      { params: { notify }, timeout: PLAN_BATCH_EVALUATION_TIMEOUT_MS },
    );
    return toCamelCase<InvestmentPlanBatchEvaluationResponse>(response.data);
  },
};
