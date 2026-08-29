export type InvestmentStrategyType = 'index_crash' | 'swing' | 'dividend' | 'cycle' | 'value' | 'growth';
export type InvestmentPlanStatus = 'draft' | 'active' | 'paused' | 'closed';
export type InvestmentPlanStepAction = 'buy' | 'add' | 'reduce' | 'exit' | 'review';
export type InvestmentPlanStepMetric = 'price' | 'benchmark_drawdown_250d_pct';
export type InvestmentPlanStepOperator = 'lte' | 'gte' | 'between';
export type InvestmentPlanStepStatus = 'pending' | 'triggered' | 'completed' | 'skipped';
export type InvestmentPlanCheckFrequency = 'minute' | 'daily' | 'hourly' | 'manual';
export type InvestmentPlanNotificationChannel =
  | 'wechat' | 'feishu' | 'telegram' | 'email' | 'pushover' | 'ntfy' | 'gotify'
  | 'pushplus' | 'serverchan3' | 'custom' | 'discord' | 'slack' | 'astrbot';

export interface InvestmentPlanStepInput {
  action: InvestmentPlanStepAction;
  metric: InvestmentPlanStepMetric;
  operator: InvestmentPlanStepOperator;
  threshold: number;
  upperThreshold?: number | null;
  targetPositionPct?: number | null;
  note?: string | null;
}

export interface InvestmentPlanStepItem extends InvestmentPlanStepInput {
  id: number;
  planId: number;
  sortOrder: number;
  status: InvestmentPlanStepStatus;
  triggeredAt?: string | null;
  completedAt?: string | null;
  executionDate?: string | null;
  executionAt?: string | null;
  executionPrice?: number | null;
  executionQuantity?: number | null;
  executionAmount?: number | null;
  executionFee?: number | null;
  executionNote?: string | null;
  notifiedAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface InvestmentPlanExecutionSummary {
  completedExecutionCount: number;
  unrecordedCompletedCount: number;
  executionDataComplete: boolean;
  plannedCapital?: number | null;
  totalQuantity: number;
  grossAmount: number;
  totalFees: number;
  totalCost: number;
  averageCost?: number | null;
  remainingCash?: number | null;
  valuationPrice?: number | null;
  valuationPriceSource?: 'plan_check' | 'latest_execution' | null;
  valuationAsOfDate?: string | null;
  marketValue?: number | null;
  unrealizedPnl?: number | null;
  returnPct?: number | null;
  targetPositionPct?: number | null;
  capitalUtilizationPct?: number | null;
  targetDeviationPct?: number | null;
}

export interface InvestmentPlanExecutionRequest {
  executionAt: string;
  price: number;
  quantity: number;
  fee?: number;
  note?: string | null;
}

export interface InvestmentPlanItem {
  id: number;
  accountId?: number | null;
  symbol: string;
  market: 'cn' | 'hk' | 'us';
  name?: string | null;
  strategyType: InvestmentStrategyType;
  strategyLabel: string;
  status: InvestmentPlanStatus;
  thesis: string;
  invalidationNote: string;
  benchmarkSymbol?: string | null;
  plannedCapital?: number | null;
  maxPositionPct?: number | null;
  requiredCashPct?: number | null;
  reviewDate?: string | null;
  notifyOnTrigger: boolean;
  notificationChannels: InvestmentPlanNotificationChannel[];
  checkFrequency: InvestmentPlanCheckFrequency;
  reviewDue: boolean;
  lastPrice?: number | null;
  lastEvaluatedAt?: string | null;
  lastEvaluationStatus?: string | null;
  lastEvaluationNote?: string | null;
  lastBlockedReasons: string[];
  triggeredStepCount: number;
  createdAt?: string | null;
  updatedAt?: string | null;
  steps: InvestmentPlanStepItem[];
  executionSummary: InvestmentPlanExecutionSummary;
}

export interface InvestmentPlanCreateRequest {
  symbol: string;
  market: 'cn' | 'hk' | 'us';
  name?: string;
  accountId?: number | null;
  strategyType: InvestmentStrategyType;
  status: 'draft' | 'active';
  thesis: string;
  invalidationNote: string;
  benchmarkSymbol?: string | null;
  plannedCapital?: number | null;
  maxPositionPct?: number | null;
  requiredCashPct?: number | null;
  reviewDate?: string | null;
  notifyOnTrigger?: boolean;
  notificationChannels?: InvestmentPlanNotificationChannel[];
  checkFrequency?: InvestmentPlanCheckFrequency;
  steps: InvestmentPlanStepInput[];
}

export type InvestmentPlanUpdateRequest = Partial<Omit<InvestmentPlanCreateRequest, 'symbol' | 'market' | 'accountId' | 'status'>>;

export interface InvestmentPlanListResponse {
  items: InvestmentPlanItem[];
  total: number;
  summary: {
    active: number;
    triggered: number;
    blocked: number;
    reviewDue: number;
    dataMissing: number;
  };
}

export interface InvestmentPlanEvaluationResponse {
  plan: InvestmentPlanItem;
  metricValues: Record<string, number | null>;
  matchedStepIds: number[];
  newlyTriggeredStepIds: number[];
  constraints: {
    positionPct?: number | null;
    cashPct?: number | null;
  };
  blockedReasons: string[];
  reviewDue: boolean;
  errors: string[];
  notification: {
    attempted?: boolean;
    sent?: boolean;
    queued?: boolean;
    stepCount?: number;
  };
}

export interface InvestmentPlanBatchEvaluationResponse {
  evaluated: number;
  triggered: number;
  errors: Array<{ planId: number; message: string }>;
  results: InvestmentPlanEvaluationResponse[];
  notification: {
    attempted?: boolean;
    sent?: boolean;
    queued?: boolean;
    stepCount?: number;
  };
}
