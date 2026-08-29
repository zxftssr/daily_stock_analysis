import type React from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Plus, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { backtestApi } from '../api/backtest';
import { getParsedApiError } from '../api/error';
import { investmentPlansApi } from '../api/investmentPlans';
import { ApiErrorAlert, Badge, Button, Card, EmptyState, Select } from '../components/common';
import { useStockIndex } from '../hooks/useStockIndex';
import type {
  EtfCrashBacktestRequest,
  EtfCrashBacktestResponse,
  EtfCrashBacktestStage,
  EtfCrashRobustnessRequest,
  EtfCrashRobustnessResponse,
} from '../types/backtest';
import type { InvestmentPlanItem } from '../types/investmentPlan';
import { toDateInputValue } from '../utils/format';

const inputClass = 'h-10 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-ring';

const today = () => toDateInputValue(new Date());
const oneYearAgo = () => {
  const value = new Date();
  value.setFullYear(value.getFullYear() - 1);
  return toDateInputValue(value);
};

const money = (value: number) => new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  maximumFractionDigits: 0,
}).format(value);

const percent = (value: number) => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;

const DEFAULT_STAGES: EtfCrashBacktestStage[] = [
  { drawdownPct: 10, targetPositionPct: 20 },
  { drawdownPct: 15, targetPositionPct: 40 },
  { drawdownPct: 20, targetPositionPct: 70 },
];

const ResultMetric: React.FC<{
  label: string;
  value: string;
  tone?: 'positive' | 'negative' | 'neutral';
}> = ({ label, value, tone = 'neutral' }) => (
  <Card variant="bordered" padding="md">
    <p className="text-xs text-muted-foreground">{label}</p>
    <p className={`mt-2 text-xl font-semibold tabular-nums ${
      tone === 'positive' ? 'text-success' : tone === 'negative' ? 'text-danger' : 'text-foreground'
    }`}>{value}</p>
  </Card>
);

const EtfCrashBacktestPage: React.FC = () => {
  const { index, loading: indexLoading } = useStockIndex();
  const etfs = useMemo(
    () => index.filter(item => item.active && item.assetType === 'etf' && item.market === 'CN'),
    [index],
  );
  const [symbol, setSymbol] = useState('510300');
  const [startDate, setStartDate] = useState(oneYearAgo);
  const [endDate, setEndDate] = useState(today);
  const [initialCapital, setInitialCapital] = useState(100000);
  const [stages, setStages] = useState<EtfCrashBacktestStage[]>(DEFAULT_STAGES);
  const [running, setRunning] = useState(false);
  const [creatingPlan, setCreatingPlan] = useState(false);
  const [robustnessRunning, setRobustnessRunning] = useState(false);
  const [result, setResult] = useState<EtfCrashBacktestResponse | null>(null);
  const [robustnessResult, setRobustnessResult] = useState<EtfCrashRobustnessResponse | null>(null);
  const [lastRequest, setLastRequest] = useState<EtfCrashBacktestRequest | null>(null);
  const [createdPlan, setCreatedPlan] = useState<InvestmentPlanItem | null>(null);
  const [error, setError] = useState<ReturnType<typeof getParsedApiError> | null>(null);
  const requestVersion = useRef(0);
  const robustnessVersion = useRef(0);
  const [comparisonSymbols, setComparisonSymbols] = useState<string[]>([]);
  const [windowTradingDays, setWindowTradingDays] = useState(60);
  const [stepTradingDays, setStepTradingDays] = useState(30);
  const [outOfSamplePct, setOutOfSamplePct] = useState(40);
  const [minWindows, setMinWindows] = useState(3);
  const [minPassRatePct, setMinPassRatePct] = useState(60);
  const [minWindowReturnPct, setMinWindowReturnPct] = useState(0);
  const [maxWindowDrawdownPct, setMaxWindowDrawdownPct] = useState(15);
  const [minTriggeredStages, setMinTriggeredStages] = useState(1);

  useEffect(() => {
    document.title = 'ETF 大跌策略回测 - DSA';
  }, []);

  useEffect(() => {
    if (!etfs.length || etfs.some(item => item.displayCode === symbol)) return;
    const preferred = etfs.find(item => item.displayCode === '510300') || etfs[0];
    setSymbol(preferred.displayCode);
  }, [etfs, symbol]);

  const invalidateResult = () => {
    requestVersion.current += 1;
    robustnessVersion.current += 1;
    setRunning(false);
    setRobustnessRunning(false);
    setResult(null);
    setRobustnessResult(null);
    setLastRequest(null);
    setCreatedPlan(null);
  };

  const invalidateRobustness = () => {
    robustnessVersion.current += 1;
    setRobustnessRunning(false);
    setRobustnessResult(null);
    setCreatedPlan(null);
  };

  const updateRobustnessNumber = (
    setter: React.Dispatch<React.SetStateAction<number>>,
    value: number,
  ) => {
    invalidateRobustness();
    setter(value);
  };

  const updateStage = (index: number, field: keyof EtfCrashBacktestStage, value: number) => {
    invalidateResult();
    setStages(current => current.map((stage, stageIndex) => (
      stageIndex === index ? { ...stage, [field]: value } : stage
    )));
  };

  const validate = (): string | null => {
    if (!symbol) return '请选择宽基 ETF';
    if (!startDate || !endDate || startDate > endDate) return '请选择有效的回测日期范围';
    if (!Number.isFinite(initialCapital) || initialCapital <= 0) return '初始资金必须大于 0';
    for (let index = 0; index < stages.length; index += 1) {
      const stage = stages[index];
      if (!(stage.drawdownPct > 0 && stage.drawdownPct <= 80)) return '回撤阈值必须在 0 至 80 之间';
      if (!(stage.targetPositionPct > 0 && stage.targetPositionPct <= 100)) return '目标仓位必须在 0 至 100 之间';
      if (index > 0 && stage.drawdownPct <= stages[index - 1].drawdownPct) return '回撤阈值必须逐档递增';
      if (index > 0 && stage.targetPositionPct <= stages[index - 1].targetPositionPct) return '目标仓位必须逐档递增';
    }
    return null;
  };

  const handleRun = async () => {
    const version = requestVersion.current + 1;
    requestVersion.current = version;
    robustnessVersion.current += 1;
    setRobustnessRunning(false);
    setResult(null);
    setRobustnessResult(null);
    setLastRequest(null);
    setCreatedPlan(null);
    const validation = validate();
    if (validation) {
      setError(getParsedApiError(new Error(validation)));
      return;
    }
    const request: EtfCrashBacktestRequest = {
      symbol,
      startDate,
      endDate,
      initialCapital,
      stages: stages.map(stage => ({ ...stage })),
    };
    setRunning(true);
    setError(null);
    try {
      const response = await backtestApi.runEtfCrash(request);
      if (version !== requestVersion.current) return;
      setResult(response);
      setLastRequest(request);
    } catch (requestError) {
      if (version !== requestVersion.current) return;
      setError(getParsedApiError(requestError));
    } finally {
      if (version === requestVersion.current) {
        setRunning(false);
      }
    }
  };

  const handleRunRobustness = async () => {
    if (!lastRequest || !result) return;
    const version = robustnessVersion.current + 1;
    robustnessVersion.current = version;
    const symbols = [symbol, ...comparisonSymbols.filter(item => item !== symbol)].slice(0, 5);
    const request: EtfCrashRobustnessRequest = {
      ...lastRequest,
      symbols,
      windowTradingDays,
      stepTradingDays,
      outOfSamplePct,
      minWindows,
      minPassRatePct,
      minWindowReturnPct,
      maxWindowDrawdownPct,
      minTriggeredStages,
    };
    setRobustnessRunning(true);
    setRobustnessResult(null);
    setCreatedPlan(null);
    setError(null);
    try {
      const response = await backtestApi.runEtfCrashRobustness(request);
      if (version !== robustnessVersion.current) return;
      setRobustnessResult(response);
    } catch (requestError) {
      if (version !== robustnessVersion.current) return;
      setError(getParsedApiError(requestError));
    } finally {
      if (version === robustnessVersion.current) {
        setRobustnessRunning(false);
      }
    }
  };

  const handleCreatePlan = async () => {
    if (!result || !lastRequest || !robustnessResult?.passed) return;
    const finalTarget = lastRequest.stages[lastRequest.stages.length - 1].targetPositionPct;
    setCreatingPlan(true);
    setError(null);
    try {
      const plan = await investmentPlansApi.create({
        symbol: result.symbol,
        market: 'cn',
        name: `${result.name}大跌分档计划`,
        strategyType: 'index_crash',
        status: 'active',
        thesis: `基于 ${result.effectiveStartDate} 至 ${result.effectiveEndDate} 的本地日线回测，分档策略收益 ${result.totalReturnPct.toFixed(2)}%，最大回撤 ${result.maxDrawdownPct.toFixed(2)}%；滚动稳健性验证 ${robustnessResult.summary.passedWindows}/${robustnessResult.summary.totalWindows} 个窗口通过。`,
        invalidationNote: 'ETF 停止跟踪目标指数、流动性显著恶化或回撤数据过期时暂停执行，并由人工重新评估。',
        benchmarkSymbol: result.symbol,
        plannedCapital: lastRequest.initialCapital,
        maxPositionPct: finalTarget,
        requiredCashPct: Math.max(0, 100 - finalTarget),
        notifyOnTrigger: true,
        checkFrequency: 'minute',
        steps: lastRequest.stages.map((stage, index) => ({
          action: index === 0 ? 'buy' : 'add',
          metric: 'benchmark_drawdown_250d_pct',
          operator: 'gte',
          threshold: stage.drawdownPct,
          targetPositionPct: stage.targetPositionPct,
          note: `250日回撤达到 ${stage.drawdownPct}% 时，将累计目标仓位提高到 ${stage.targetPositionPct}%`,
        })),
      });
      setCreatedPlan(plan);
    } catch (requestError) {
      setError(getParsedApiError(requestError));
    } finally {
      setCreatingPlan(false);
    }
  };

  return (
    <div className="min-h-full bg-background p-4 sm:p-6">
      <div className="mx-auto max-w-[1680px] space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Badge variant="info">SQLite 本地日线</Badge>
              <Badge variant="default">250 日高点回撤</Badge>
            </div>
            <h1 className="mt-2 text-2xl font-semibold text-foreground">ETF 大跌策略回测</h1>
            <p className="mt-1 text-sm text-muted-foreground">校准分档回撤与累计目标仓位，回测后可直接创建无账户策略计划。</p>
          </div>
          <Link to="/backtest" className="inline-flex h-9 items-center gap-2 rounded-md border border-input px-3 text-sm text-foreground hover:bg-accent">
            <ArrowLeft className="h-4 w-4" /> AI 分析回测
          </Link>
        </div>

        {error ? <ApiErrorAlert error={error} /> : null}
        {createdPlan ? (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-success/40 bg-success/10 px-4 py-3 text-sm text-foreground">
            <span>策略计划 #{createdPlan.id} 已创建并启用，达到回撤档位后将进入通知链路。</span>
            <Link to="/plans" className="font-medium text-primary hover:underline">查看策略计划</Link>
          </div>
        ) : null}

        <div className="grid gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
          <Card variant="bordered" padding="lg" className="h-fit">
            <h2 className="text-base font-semibold text-foreground">回测参数</h2>
            <div className="mt-4 space-y-4">
              <Select
                label="宽基 ETF"
                value={symbol}
                onChange={(value) => {
                  invalidateResult();
                  setSymbol(value);
                }}
                disabled={indexLoading}
                options={etfs.map(item => ({ value: item.displayCode, label: `${item.nameZh} · ${item.displayCode}` }))}
              />
              <div className="grid grid-cols-2 gap-3">
                <label className="text-sm text-foreground">
                  <span className="mb-2 block font-medium">开始日期</span>
                  <input aria-label="回测开始日期" type="date" value={startDate} onChange={event => {
                    invalidateResult();
                    setStartDate(event.target.value);
                  }} className={inputClass} />
                </label>
                <label className="text-sm text-foreground">
                  <span className="mb-2 block font-medium">结束日期</span>
                  <input aria-label="回测结束日期" type="date" value={endDate} onChange={event => {
                    invalidateResult();
                    setEndDate(event.target.value);
                  }} className={inputClass} />
                </label>
              </div>
              <label className="text-sm text-foreground">
                <span className="mb-2 block font-medium">初始资金</span>
                <input aria-label="初始资金" type="number" min={1} value={initialCapital} onChange={event => {
                  invalidateResult();
                  setInitialCapital(Number(event.target.value));
                }} className={inputClass} />
              </label>

              <div>
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium text-foreground">买入档位</h3>
                  <Button
                    size="xsm"
                    variant="outline"
                    disabled={stages.length >= 6}
                    onClick={() => {
                      invalidateResult();
                      setStages(current => [
                        ...current,
                        {
                          drawdownPct: Math.min(80, current[current.length - 1].drawdownPct + 5),
                          targetPositionPct: Math.min(100, current[current.length - 1].targetPositionPct + 10),
                        },
                      ]);
                    }}
                  >
                    <Plus className="h-3.5 w-3.5" /> 添加
                  </Button>
                </div>
                <div className="mt-2 space-y-2">
                  {stages.map((stage, index) => (
                    <div key={index} className="grid grid-cols-[1fr_1fr_auto] items-end gap-2 rounded-md border border-border/60 p-2">
                      <label className="text-xs text-muted-foreground">
                        回撤 %
                        <input aria-label={`第 ${index + 1} 档回撤`} type="number" min={0.1} max={80} step={0.1} value={stage.drawdownPct} onChange={event => updateStage(index, 'drawdownPct', Number(event.target.value))} className={`${inputClass} mt-1`} />
                      </label>
                      <label className="text-xs text-muted-foreground">
                        累计仓位 %
                        <input aria-label={`第 ${index + 1} 档仓位`} type="number" min={0.1} max={100} step={0.1} value={stage.targetPositionPct} onChange={event => updateStage(index, 'targetPositionPct', Number(event.target.value))} className={`${inputClass} mt-1`} />
                      </label>
                      <Button size="sm" variant="ghost" aria-label={`删除第 ${index + 1} 档`} disabled={stages.length === 1} onClick={() => {
                        invalidateResult();
                        setStages(current => {
                          const next = current.filter((_, stageIndex) => stageIndex !== index);
                          setMinTriggeredStages(value => Math.min(value, next.length));
                          return next;
                        });
                      }}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              </div>

              <Button className="w-full" isLoading={running} loadingText="回测中" onClick={() => void handleRun()}>
                运行 ETF 策略回测
              </Button>
              <p className="text-xs leading-5 text-muted-foreground">只使用已预热到 SQLite 的日线；不包含手续费、滑点、分红和自动下单。</p>

              <div className="rounded-lg border border-border/70 bg-muted/20 p-3">
                <div>
                  <h3 className="text-sm font-semibold text-foreground">稳健性验证</h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">固定当前档位，在滚动窗口和可选对照 ETF 上验证；不会自动寻找最优参数。</p>
                </div>
                <label className="mt-3 block text-xs text-muted-foreground">
                  对照 ETF（最多再选 4 只）
                  <select
                    multiple
                    aria-label="稳健性对照 ETF"
                    value={comparisonSymbols}
                    onChange={event => {
                      invalidateRobustness();
                      setComparisonSymbols(Array.from(event.target.selectedOptions)
                        .map(option => option.value)
                        .slice(0, 4));
                    }}
                    className={`${inputClass} mt-1 h-24 py-2`}
                  >
                    {etfs.filter(item => item.displayCode !== symbol).map(item => (
                      <option key={item.displayCode} value={item.displayCode}>{item.nameZh} · {item.displayCode}</option>
                    ))}
                  </select>
                </label>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  <label className="text-xs text-muted-foreground">窗口交易日
                    <input aria-label="窗口交易日" type="number" min={20} max={500} value={windowTradingDays} onChange={event => updateRobustnessNumber(setWindowTradingDays, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">滚动步长
                    <input aria-label="滚动步长" type="number" min={1} max={windowTradingDays} value={stepTradingDays} onChange={event => updateRobustnessNumber(setStepTradingDays, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">样本外占比 %
                    <input aria-label="样本外占比" type="number" min={10} max={80} value={outOfSamplePct} onChange={event => updateRobustnessNumber(setOutOfSamplePct, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">最少窗口
                    <input aria-label="最少有效窗口" type="number" min={2} max={100} value={minWindows} onChange={event => updateRobustnessNumber(setMinWindows, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">最低通过率 %
                    <input aria-label="最低通过率" type="number" min={0} max={100} value={minPassRatePct} onChange={event => updateRobustnessNumber(setMinPassRatePct, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">窗口最低收益 %
                    <input aria-label="窗口最低收益" type="number" min={-100} max={1000} step={0.1} value={minWindowReturnPct} onChange={event => updateRobustnessNumber(setMinWindowReturnPct, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">窗口最大回撤 %
                    <input aria-label="窗口最大回撤" type="number" min={0} max={100} step={0.1} value={maxWindowDrawdownPct} onChange={event => updateRobustnessNumber(setMaxWindowDrawdownPct, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                  <label className="text-xs text-muted-foreground">最少触发档位
                    <input aria-label="最少触发档位" type="number" min={0} max={stages.length} value={minTriggeredStages} onChange={event => updateRobustnessNumber(setMinTriggeredStages, Number(event.target.value))} className={`${inputClass} mt-1`} />
                  </label>
                </div>
                <Button
                  className="mt-3 w-full"
                  variant="secondary"
                  disabled={!result}
                  isLoading={robustnessRunning}
                  loadingText="验证中"
                  onClick={() => void handleRunRobustness()}
                >
                  运行稳健性验证
                </Button>
                {!result ? <p className="mt-2 text-xs text-muted-foreground">请先运行当前参数的单次回测。</p> : null}
              </div>
            </div>
          </Card>

          <section className="min-w-0 space-y-4">
            {!result ? (
              <EmptyState title="等待回测" description="选择 ETF 和回撤档位后运行回测，结果不会写入交易账户。" className="min-h-[360px] border-dashed" />
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3">
                  <div>
                    <h2 className="font-semibold text-foreground">{result.name} · {result.symbol}</h2>
                    <p className="mt-1 text-xs text-muted-foreground">有效区间 {result.effectiveStartDate} 至 {result.effectiveEndDate} · {result.tradingDays} 个交易日 · 数据代码 {result.storageCode}</p>
                  </div>
                  <Button disabled={!robustnessResult?.passed} isLoading={creatingPlan} loadingText="创建中" onClick={() => void handleCreatePlan()}>
                    {robustnessResult?.passed ? '一键创建并启用策略' : '先通过稳健性验证'}
                  </Button>
                </div>

                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  <ResultMetric label="策略收益" value={percent(result.totalReturnPct)} tone={result.totalReturnPct >= 0 ? 'positive' : 'negative'} />
                  <ResultMetric label="同期买入持有" value={percent(result.buyHoldReturnPct)} tone={result.buyHoldReturnPct >= 0 ? 'positive' : 'negative'} />
                  <ResultMetric label="组合最大回撤" value={percent(-result.maxDrawdownPct)} tone="negative" />
                  <ResultMetric label="平均资金利用率" value={`${result.capitalUtilizationPct.toFixed(2)}%`} />
                  <ResultMetric label="期末权益" value={money(result.finalEquity)} />
                  <ResultMetric label="触发档位" value={`${result.triggeredStageCount}/${result.stages.length}`} />
                  <ResultMetric label="首次等待" value={`${result.firstTriggerWaitTradingDays} 个交易日`} />
                  <ResultMetric label="最长等待" value={`${result.longestWaitTradingDays} 个交易日`} />
                </div>

                {robustnessResult ? (
                  <Card variant="bordered" padding="lg">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="font-semibold text-foreground">滚动稳健性结论</h3>
                          <Badge variant={robustnessResult.passed ? 'success' : 'danger'}>
                            {robustnessResult.passed ? '通过' : '未通过'}
                          </Badge>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">固定参数 · {robustnessResult.eligibleSymbols.length}/{robustnessResult.requestedSymbols.length} 只 ETF 有效</p>
                      </div>
                      <div className="text-right text-xs text-muted-foreground">
                        <p>总体 {robustnessResult.summary.passedWindows}/{robustnessResult.summary.totalWindows} · {robustnessResult.summary.passRatePct.toFixed(1)}%</p>
                        <p>样本外 {robustnessResult.summary.outOfSamplePassedWindows}/{robustnessResult.summary.outOfSampleWindows} · {robustnessResult.summary.outOfSamplePassRatePct.toFixed(1)}%</p>
                      </div>
                    </div>
                    {robustnessResult.failureReasons.length ? (
                      <div className="mt-3 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                        {robustnessResult.failureReasons.join('；')}
                      </div>
                    ) : null}
                    <div className="mt-3 grid gap-2 sm:grid-cols-3">
                      <div className="rounded-md bg-muted/40 p-3"><p className="text-xs text-muted-foreground">平均 / 最差收益</p><p className="mt-1 font-semibold text-foreground">{robustnessResult.summary.averageReturnPct?.toFixed(2) ?? '--'}% / {robustnessResult.summary.worstReturnPct?.toFixed(2) ?? '--'}%</p></div>
                      <div className="rounded-md bg-muted/40 p-3"><p className="text-xs text-muted-foreground">最差最大回撤</p><p className="mt-1 font-semibold text-foreground">{robustnessResult.summary.worstMaxDrawdownPct?.toFixed(2) ?? '--'}%</p></div>
                      <div className="rounded-md bg-muted/40 p-3"><p className="text-xs text-muted-foreground">档位触发覆盖</p><p className="mt-1 font-semibold text-foreground">{robustnessResult.summary.triggerCoveragePct.toFixed(2)}%</p></div>
                    </div>
                    <div className="mt-4 overflow-x-auto">
                      <table className="w-full min-w-[820px] text-sm">
                        <thead className="border-b border-border text-left text-xs text-muted-foreground">
                          <tr><th className="py-2">ETF</th><th>样本</th><th>窗口</th><th>收益</th><th>最大回撤</th><th>资金利用率</th><th>触发档位</th><th>结论</th></tr>
                        </thead>
                        <tbody>
                          {robustnessResult.windows.map(item => (
                            <tr key={`${item.symbol}-${item.windowIndex}`} className="border-b border-border/50 text-foreground">
                              <td className="py-3">{item.name}<span className="ml-1 text-xs text-muted-foreground">{item.symbol}</span></td>
                              <td><Badge variant={item.sampleType === 'out_of_sample' ? 'info' : 'default'}>{item.sampleType === 'out_of_sample' ? '样本外' : '样本内'}</Badge></td>
                              <td>{item.startDate} → {item.endDate}</td>
                              <td>{percent(item.totalReturnPct)}</td>
                              <td>{item.maxDrawdownPct.toFixed(2)}%</td>
                              <td>{item.capitalUtilizationPct.toFixed(2)}%</td>
                              <td>{item.triggeredStageCount}</td>
                              <td><Badge variant={item.passed ? 'success' : 'danger'}>{item.passed ? '通过' : item.failureReasons.join(' / ')}</Badge></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Card>
                ) : null}

                <Card variant="bordered" padding="lg">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="font-semibold text-foreground">触发记录</h3>
                    <span className="text-xs text-muted-foreground">平均买入价 {result.averageEntryPrice == null ? '--' : result.averageEntryPrice.toFixed(4)}</span>
                  </div>
                  {result.trades.length === 0 ? (
                    <p className="mt-4 text-sm text-muted-foreground">回测区间内没有达到任何回撤档位。</p>
                  ) : (
                    <div className="mt-3 overflow-x-auto">
                      <table className="w-full min-w-[720px] text-sm">
                        <thead className="border-b border-border text-left text-xs text-muted-foreground">
                          <tr><th className="py-2">日期</th><th>动作</th><th>实际回撤</th><th>触发档</th><th>目标仓位</th><th>成交价</th><th>剩余现金</th></tr>
                        </thead>
                        <tbody>
                          {result.trades.map((trade, index) => (
                            <tr key={`${trade.date}-${index}`} className="border-b border-border/50 text-foreground">
                              <td className="py-3">{trade.date}</td>
                              <td>{trade.action === 'buy' ? '买入' : '加仓'}</td>
                              <td>{trade.drawdownPct.toFixed(2)}%</td>
                              <td>{trade.thresholdPct.toFixed(2)}%</td>
                              <td>{trade.targetPositionPct.toFixed(2)}%</td>
                              <td>{trade.price.toFixed(4)}</td>
                              <td>{money(trade.cashAfter)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
};

export default EtfCrashBacktestPage;
