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
  const [result, setResult] = useState<EtfCrashBacktestResponse | null>(null);
  const [lastRequest, setLastRequest] = useState<EtfCrashBacktestRequest | null>(null);
  const [createdPlan, setCreatedPlan] = useState<InvestmentPlanItem | null>(null);
  const [error, setError] = useState<ReturnType<typeof getParsedApiError> | null>(null);
  const requestVersion = useRef(0);

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
    setRunning(false);
    setResult(null);
    setLastRequest(null);
    setCreatedPlan(null);
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
    setResult(null);
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

  const handleCreatePlan = async () => {
    if (!result || !lastRequest) return;
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
        thesis: `基于 ${result.effectiveStartDate} 至 ${result.effectiveEndDate} 的本地日线回测，分档策略收益 ${result.totalReturnPct.toFixed(2)}%，最大回撤 ${result.maxDrawdownPct.toFixed(2)}%。`,
        invalidationNote: 'ETF 停止跟踪目标指数、流动性显著恶化或回撤数据过期时暂停执行，并由人工重新评估。',
        benchmarkSymbol: result.symbol,
        maxPositionPct: finalTarget,
        requiredCashPct: Math.max(0, 100 - finalTarget),
        notifyOnTrigger: true,
        checkFrequency: 'daily',
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
                        setStages(current => current.filter((_, stageIndex) => stageIndex !== index));
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
                  <Button isLoading={creatingPlan} loadingText="创建中" onClick={() => void handleCreatePlan()}>
                    一键创建并启用策略
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
