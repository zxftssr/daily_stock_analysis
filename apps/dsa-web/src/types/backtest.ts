/**
 * Backtest API type definitions
 * Mirrors api/v1/schemas/backtest.py
 */

// ============ Request / Response ============

export interface BacktestRunRequest {
  code?: string;
  force?: boolean;
  evalWindowDays?: number;
  minAgeDays?: number;
  limit?: number;
}

export interface BacktestRunResponse {
  processed: number;
  saved: number;
  completed: number;
  insufficient: number;
  errors: number;
}

export interface EtfCrashBacktestStage {
  drawdownPct: number;
  targetPositionPct: number;
}

export interface EtfCrashBacktestRequest {
  symbol: string;
  startDate: string;
  endDate: string;
  initialCapital: number;
  stages: EtfCrashBacktestStage[];
}

export interface EtfCrashBacktestTrade {
  date: string;
  action: 'buy' | 'add';
  drawdownPct: number;
  thresholdPct: number;
  targetPositionPct: number;
  price: number;
  shares: number;
  cashAfter: number;
  positionPct: number;
}

export interface EtfCrashEquityPoint {
  date: string;
  equity: number;
  drawdownPct: number;
  positionPct: number;
}

export interface EtfCrashBacktestResponse {
  symbol: string;
  canonicalCode: string;
  name: string;
  benchmarkCode?: string | null;
  benchmarkName?: string | null;
  source: string;
  storageCode: string;
  requestedStartDate: string;
  requestedEndDate: string;
  effectiveStartDate: string;
  effectiveEndDate: string;
  tradingDays: number;
  initialCapital: number;
  finalEquity: number;
  cashRemaining: number;
  positionValue: number;
  totalReturnPct: number;
  buyHoldReturnPct: number;
  excessReturnPct: number;
  maxDrawdownPct: number;
  capitalUtilizationPct: number;
  maxPositionPct: number;
  triggerCount: number;
  triggeredStageCount: number;
  untriggeredStageCount: number;
  firstTriggerWaitTradingDays: number;
  longestWaitTradingDays: number;
  averageEntryPrice?: number | null;
  stages: EtfCrashBacktestStage[];
  trades: EtfCrashBacktestTrade[];
  equityCurve: EtfCrashEquityPoint[];
}

// ============ Result Item ============

export interface BacktestResultItem {
  analysisHistoryId: number;
  code: string;
  stockName?: string;
  analysisDate?: string;
  evalWindowDays: number;
  engineVersion: string;
  evalStatus: string;
  evaluatedAt?: string;
  operationAdvice?: string;
  trendPrediction?: string;
  positionRecommendation?: string;
  startPrice?: number;
  endClose?: number;
  maxHigh?: number;
  minLow?: number;
  stockReturnPct?: number;
  actualReturnPct?: number;
  actualMovement?: string;
  directionExpected?: string;
  directionCorrect?: boolean;
  outcome?: string;
  stopLoss?: number;
  takeProfit?: number;
  hitStopLoss?: boolean;
  hitTakeProfit?: boolean;
  firstHit?: string;
  firstHitDate?: string;
  firstHitTradingDays?: number;
  simulatedEntryPrice?: number;
  simulatedExitPrice?: number;
  simulatedExitReason?: string;
  simulatedReturnPct?: number;
}

export interface BacktestResultsResponse {
  total: number;
  page: number;
  limit: number;
  items: BacktestResultItem[];
}

// ============ Performance Metrics ============

export interface PerformanceMetrics {
  scope: string;
  code?: string;
  evalWindowDays: number;
  engineVersion: string;
  computedAt?: string;

  totalEvaluations: number;
  completedCount: number;
  insufficientCount: number;
  longCount: number;
  cashCount: number;
  winCount: number;
  lossCount: number;
  neutralCount: number;

  directionAccuracyPct?: number;
  winRatePct?: number;
  neutralRatePct?: number;
  avgStockReturnPct?: number;
  avgSimulatedReturnPct?: number;

  stopLossTriggerRate?: number;
  takeProfitTriggerRate?: number;
  ambiguousRate?: number;
  avgDaysToFirstHit?: number;

  adviceBreakdown: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
}
