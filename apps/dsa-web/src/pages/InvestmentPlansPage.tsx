import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BellRing,
  CalendarClock,
  Check,
  CirclePause,
  ClipboardList,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  SkipForward,
  Trash2,
  X,
} from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { investmentPlansApi } from '../api/investmentPlans';
import { portfolioApi } from '../api/portfolio';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  Drawer,
  EmptyState,
  InlineAlert,
  Input,
  Select,
} from '../components/common';
import type { PortfolioAccountItem } from '../types/portfolio';
import type {
  InvestmentPlanCreateRequest,
  InvestmentPlanCheckFrequency,
  InvestmentPlanExecutionRequest,
  InvestmentPlanItem,
  InvestmentPlanNotificationChannel,
  InvestmentPlanStatus,
  InvestmentPlanStepAction,
  InvestmentPlanStepInput,
  InvestmentPlanStepItem,
  InvestmentPlanStepMetric,
  InvestmentPlanStepOperator,
  InvestmentPlanStepStatus,
  InvestmentStrategyType,
} from '../types/investmentPlan';
import { cn } from '../utils/cn';

const STRATEGY_OPTIONS: Array<{ value: InvestmentStrategyType; label: string }> = [
  { value: 'index_crash', label: '指数大跌' },
  { value: 'swing', label: '波段交易' },
  { value: 'dividend', label: '股息收息' },
  { value: 'cycle', label: '周期布局' },
  { value: 'value', label: '价值投资' },
  { value: 'growth', label: '成长投资' },
];

const STATUS_OPTIONS = [
  { value: 'current', label: '当前计划' },
  { value: '', label: '全部状态' },
  { value: 'draft', label: '草稿' },
  { value: 'active', label: '执行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'closed', label: '已移除' },
];

const MARKET_OPTIONS = [
  { value: 'cn', label: 'A 股 / 北交所' },
  { value: 'hk', label: '港股' },
  { value: 'us', label: '美股' },
];

const MARKET_CURRENCY = {
  cn: 'CNY',
  hk: 'HKD',
  us: 'USD',
} as const;

const ACTION_OPTIONS = [
  { value: 'buy', label: '买入' },
  { value: 'add', label: '加仓' },
  { value: 'reduce', label: '减仓' },
  { value: 'exit', label: '退出 / 复查' },
  { value: 'review', label: '人工复查' },
];

const METRIC_OPTIONS = [
  { value: 'price', label: '标的价格' },
  { value: 'benchmark_drawdown_250d_pct', label: '基准250日回撤' },
];

const OPERATOR_OPTIONS = [
  { value: 'lte', label: '小于等于' },
  { value: 'gte', label: '大于等于' },
  { value: 'between', label: '进入区间' },
];

const CHECK_FREQUENCY_OPTIONS: Array<{ value: InvestmentPlanCheckFrequency; label: string }> = [
  { value: 'minute', label: '盘中高频（每分钟）' },
  { value: 'daily', label: '每日定时任务' },
  { value: 'hourly', label: '每小时（schedule 模式）' },
  { value: 'manual', label: '仅手工检查' },
];

const NOTIFICATION_CHANNEL_OPTIONS: Array<{ value: '' | InvestmentPlanNotificationChannel; label: string }> = [
  { value: '', label: '使用全局告警渠道' },
  { value: 'wechat', label: '企业微信' },
  { value: 'feishu', label: '飞书' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'email', label: '邮件' },
  { value: 'pushover', label: 'Pushover' },
  { value: 'ntfy', label: 'ntfy' },
  { value: 'gotify', label: 'Gotify' },
  { value: 'pushplus', label: 'PushPlus' },
  { value: 'serverchan3', label: 'Server酱3' },
  { value: 'custom', label: '自定义 Webhook' },
  { value: 'discord', label: 'Discord' },
  { value: 'slack', label: 'Slack' },
  { value: 'astrbot', label: 'AstrBot' },
];

const STATUS_META: Record<InvestmentPlanStatus, { label: string; variant: 'default' | 'success' | 'warning' | 'info' }> = {
  draft: { label: '草稿', variant: 'default' },
  active: { label: '执行中', variant: 'success' },
  paused: { label: '已暂停', variant: 'warning' },
  closed: { label: '已移除', variant: 'default' },
};

const STEP_STATUS_META: Record<InvestmentPlanStepStatus, { label: string; tone: string; dot: string }> = {
  pending: { label: '等待', tone: 'text-muted-foreground', dot: 'border-border bg-background' },
  triggered: { label: '已触发', tone: 'text-warning', dot: 'border-warning bg-warning' },
  completed: { label: '已完成', tone: 'text-success', dot: 'border-success bg-success' },
  skipped: { label: '已跳过', tone: 'text-muted-foreground', dot: 'border-muted-foreground bg-muted' },
};

const EVALUATION_STATUS_LABELS: Record<string, string> = {
  waiting: '等待条件',
  triggered: '存在待处理触发',
  blocked: '受纪律阻止',
  review_due: '到期复查',
  data_missing: '数据不足',
  completed: '档位已处理',
};

const reconcileTimedOutEvaluation = async (
  planId: number,
  baselineLastEvaluatedAt?: string | null,
) => {
  try {
    const current = await investmentPlansApi.get(planId);
    if (current.lastEvaluatedAt && current.lastEvaluatedAt !== baselineLastEvaluatedAt) {
      return current;
    }
  } catch {
    // Keep the original timeout visible when reconciliation cannot read the plan.
  }
  return null;
};

type FormStep = {
  key: string;
  action: InvestmentPlanStepAction;
  metric: InvestmentPlanStepMetric;
  operator: InvestmentPlanStepOperator;
  threshold: string;
  upperThreshold: string;
  targetPositionPct: string;
  note: string;
};

type PlanForm = {
  symbol: string;
  market: 'cn' | 'hk' | 'us';
  name: string;
  accountId: string;
  strategyType: InvestmentStrategyType;
  thesis: string;
  invalidationNote: string;
  benchmarkSymbol: string;
  plannedCapital: string;
  maxPositionPct: string;
  requiredCashPct: string;
  reviewDate: string;
  notifyOnTrigger: boolean;
  notificationChannel: '' | InvestmentPlanNotificationChannel;
  checkFrequency: InvestmentPlanCheckFrequency;
  steps: FormStep[];
};

const newStep = (overrides: Partial<Omit<FormStep, 'key'>> = {}): FormStep => ({
  key: `${Date.now()}-${Math.random()}`,
  action: 'buy',
  metric: 'price',
  operator: 'lte',
  threshold: '',
  upperThreshold: '',
  targetPositionPct: '',
  note: '',
  ...overrides,
});

type StrategyTemplate = Pick<
  PlanForm,
  'strategyType' | 'thesis' | 'invalidationNote' | 'benchmarkSymbol'
  | 'maxPositionPct' | 'requiredCashPct' | 'steps'
>;

const strategyTemplate = (strategyType: InvestmentStrategyType): StrategyTemplate => {
  const templates: Record<InvestmentStrategyType, Omit<StrategyTemplate, 'strategyType'>> = {
    index_crash: {
      thesis: '宽基指数发生历史级回撤时分批布局，只使用长期闲置资金等待均值回归。',
      invalidationNote: '跟踪指数或 ETF 合约发生实质变化，或这笔资金不再满足长期闲置条件。',
      benchmarkSymbol: '000300',
      maxPositionPct: '30',
      requiredCashPct: '30',
      steps: [
        newStep({ metric: 'benchmark_drawdown_250d_pct', operator: 'gte', threshold: '20', targetPositionPct: '10', note: '首批买入，成交前确认产品流动性与跟踪误差' }),
        newStep({ action: 'add', metric: 'benchmark_drawdown_250d_pct', operator: 'gte', threshold: '30', targetPositionPct: '20', note: '第二批加仓，仍保留应急现金' }),
      ],
    },
    swing: {
      thesis: '只围绕已充分研究的标的做价格波段，上涨减仓、回落加仓，始终保留现金。',
      invalidationNote: '基本面或长期交易逻辑恶化，不再适合继续通过波段降低成本。',
      benchmarkSymbol: '',
      maxPositionPct: '25',
      requiredCashPct: '30',
      steps: [
        newStep({ action: 'add', note: '填写回落加仓价，不追涨' }),
        newStep({ action: 'reduce', operator: 'gte', targetPositionPct: '', note: '填写上涨减仓价，保留底仓' }),
      ],
    },
    dividend: {
      thesis: '分红政策稳定、现金流可持续，在目标股息率对应的安全价格下收集股份。',
      invalidationNote: '分红能力或意愿持续下降，负债与现金流已不支持原有分红逻辑。',
      benchmarkSymbol: '',
      maxPositionPct: '20',
      requiredCashPct: '25',
      steps: [newStep({ note: '填写与目标股息率匹配的最高买入价' })],
    },
    cycle: {
      thesis: '行业处于低关注、低估值的周期底部区域，供需或盈利指标存在改善可能。',
      invalidationNote: '供需格局继续恶化，或预先设定的周期改善指标未按期出现。',
      benchmarkSymbol: '',
      maxPositionPct: '20',
      requiredCashPct: '30',
      steps: [newStep({ note: '填写周期底部估值对应的分批买入价' })],
    },
    value: {
      thesis: '公司具备可验证的竞争优势与现金流，当市场情绪带来足够安全边际时分批买入。',
      invalidationNote: '竞争优势、现金流或管理层资本配置发生持续性恶化。',
      benchmarkSymbol: '',
      maxPositionPct: '20',
      requiredCashPct: '25',
      steps: [newStep({ note: '填写满足安全边际的最高买入价' })],
    },
    growth: {
      thesis: '公司仍处于可验证的高成长阶段，行业空间、产品壁垒与单位经济性支持未来扩张。',
      invalidationNote: '成长驱动指标连续低于预期，或竞争格局与融资能力不再支持原有成长路径。',
      benchmarkSymbol: '',
      maxPositionPct: '15',
      requiredCashPct: '35',
      steps: [newStep({ note: '填写估值与成长验证同时满足时的买入价' })],
    },
  };
  return { strategyType, ...templates[strategyType] };
};

const emptyForm = (): PlanForm => ({
  symbol: '',
  market: 'cn',
  name: '',
  accountId: '',
  plannedCapital: '',
  reviewDate: '',
  notifyOnTrigger: true,
  notificationChannel: '',
  checkFrequency: 'daily',
  ...strategyTemplate('value'),
});

const toOptionalNumber = (value: string): number | null => {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
};

const formatNumber = (value?: number | null, digits = 2) => {
  if (value == null || Number.isNaN(value)) return '--';
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(value);
};

const formatMoney = (
  value: number | null | undefined,
  market: InvestmentPlanItem['market'],
) => `${MARKET_CURRENCY[market]} ${formatNumber(value)}`;

const formatDateTime = (value?: string | null) => {
  if (!value) return '尚未检查';
  const dateValue = new Date(value);
  return Number.isNaN(dateValue.getTime()) ? value : dateValue.toLocaleString('zh-CN', { hour12: false });
};

const currentDateTimeInputValue = () => {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  const hour = String(now.getHours()).padStart(2, '0');
  const minute = String(now.getMinutes()).padStart(2, '0');
  const second = String(now.getSeconds()).padStart(2, '0');
  return `${year}-${month}-${day}T${hour}:${minute}:${second}`;
};

type ExecutionTarget = {
  plan: InvestmentPlanItem;
  step: InvestmentPlanStepItem;
};

type ExecutionForm = {
  executionAt: string;
  price: string;
  quantity: string;
  fee: string;
  note: string;
};

const conditionText = (step: InvestmentPlanStepInput) => {
  const metric = step.metric === 'price' ? '价格' : '基准回撤';
  const suffix = step.metric === 'price' ? '' : '%';
  if (step.operator === 'between') {
    return `${metric} ${formatNumber(step.threshold)}–${formatNumber(step.upperThreshold)}${suffix}`;
  }
  return `${metric} ${step.operator === 'lte' ? '≤' : '≥'} ${formatNumber(step.threshold)}${suffix}`;
};

const actionLabel = (action: InvestmentPlanStepAction) => (
  ACTION_OPTIONS.find((option) => option.value === action)?.label ?? action
);

const suggestedExecutionAmount = (
  plan: InvestmentPlanItem,
  step: InvestmentPlanStepItem,
): number | null => {
  if (plan.accountId != null || !plan.executionSummary.executionDataComplete) return null;
  const remainingCash = plan.executionSummary.remainingCash;
  const marketValue = plan.executionSummary.marketValue;
  const targetPositionPct = step.targetPositionPct;
  if (remainingCash == null || marketValue == null || targetPositionPct == null) return null;
  const currentEquity = remainingCash + marketValue;
  if (currentEquity <= 0) return null;
  return Math.max(0, currentEquity * targetPositionPct / 100 - marketValue);
};

const isSettingsOnlyPlan = (plan: InvestmentPlanItem) => (
  plan.status === 'active' || plan.steps.some((step) => step.status !== 'pending')
);

const parsePrefillMarket = (raw: string | null): PlanForm['market'] => {
  const value = (raw || '').toUpperCase();
  if (value === 'HK') return 'hk';
  if (value === 'US') return 'us';
  return 'cn';
};

const InvestmentPlansPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [plans, setPlans] = useState<InvestmentPlanItem[]>([]);
  const [accounts, setAccounts] = useState<PortfolioAccountItem[]>([]);
  const [summary, setSummary] = useState({ active: 0, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 0 });
  const [statusFilter, setStatusFilter] = useState('current');
  const [strategyFilter, setStrategyFilter] = useState('');
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [notice, setNotice] = useState<{ variant: 'info' | 'success' | 'warning'; title: string; message: string } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingPlan, setEditingPlan] = useState<InvestmentPlanItem | null>(null);
  const [form, setForm] = useState<PlanForm>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [evaluatingId, setEvaluatingId] = useState<number | 'all' | null>(null);
  const [uncertainEvaluations, setUncertainEvaluations] = useState<Map<number, string | null>>(
    () => new Map(),
  );
  const [transitioningId, setTransitioningId] = useState<number | null>(null);
  const [closePlan, setClosePlan] = useState<InvestmentPlanItem | null>(null);
  const [executionTarget, setExecutionTarget] = useState<ExecutionTarget | null>(null);
  const [executionForm, setExecutionForm] = useState<ExecutionForm>({
    executionAt: currentDateTimeInputValue(),
    price: '',
    quantity: '',
    fee: '0',
    note: '',
  });
  const [executionError, setExecutionError] = useState<string | null>(null);
  const [savingExecution, setSavingExecution] = useState(false);

  useEffect(() => {
    document.title = '策略计划 - DSA';
  }, []);

  const loadPlans = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      const response = await investmentPlansApi.list({
        status: statusFilter && statusFilter !== 'current'
          ? statusFilter as InvestmentPlanStatus
          : undefined,
        strategyType: strategyFilter ? strategyFilter as InvestmentStrategyType : undefined,
      });
      const loadedItems = response.items || [];
      const items = statusFilter === 'current'
        ? loadedItems.filter((plan) => plan.status !== 'closed')
        : loadedItems;
      setPlans(items);
      setSummary(response.summary || { active: 0, triggered: 0, blocked: 0, reviewDue: 0, dataMissing: 0 });
      setUncertainEvaluations((current) => {
        let changed = false;
        const next = new Map(current);
        for (const item of items) {
          const baseline = next.get(item.id);
          if (next.has(item.id) && item.lastEvaluatedAt && item.lastEvaluatedAt !== baseline) {
            next.delete(item.id);
            changed = true;
          }
        }
        return changed ? next : current;
      });
      setError(null);
      return true;
    } catch (requestError) {
      setError(getParsedApiError(requestError));
      return false;
    } finally {
      setLoading(false);
    }
  }, [statusFilter, strategyFilter]);

  useEffect(() => {
    void loadPlans();
  }, [loadPlans]);

  useEffect(() => {
    void portfolioApi.getAccounts(false).then((response) => {
      setAccounts(response.accounts || []);
    }).catch(() => setAccounts([]));
  }, []);

  const openCreate = useCallback((prefill?: Partial<PlanForm>) => {
    setEditingPlan(null);
    setForm({ ...emptyForm(), ...prefill });
    setFormError(null);
    setDrawerOpen(true);
  }, []);

  useEffect(() => {
    const symbol = searchParams.get('symbol');
    if (!symbol || drawerOpen) return;
    const requestedStrategy = searchParams.get('strategyType') as InvestmentStrategyType | null;
    const strategyType = requestedStrategy && STRATEGY_OPTIONS.some((option) => option.value === requestedStrategy)
      ? requestedStrategy
      : null;
    const template = strategyType ? strategyTemplate(strategyType) : null;
    openCreate({
      ...(template || {}),
      symbol,
      name: searchParams.get('name') || '',
      market: parsePrefillMarket(searchParams.get('market')),
      accountId: searchParams.get('accountId') || '',
      benchmarkSymbol: searchParams.get('benchmarkSymbol') || template?.benchmarkSymbol || '',
    });
    const next = new URLSearchParams(searchParams);
    next.delete('symbol');
    next.delete('name');
    next.delete('market');
    next.delete('accountId');
    next.delete('strategyType');
    next.delete('benchmarkSymbol');
    setSearchParams(next, { replace: true });
  }, [drawerOpen, openCreate, searchParams, setSearchParams]);

  const visiblePlans = useMemo(() => {
    const query = keyword.trim().toUpperCase();
    if (!query) return plans;
    return plans.filter((plan) => (
      plan.symbol.toUpperCase().includes(query)
      || (plan.name || '').toUpperCase().includes(query)
      || plan.thesis.toUpperCase().includes(query)
    ));
  }, [keyword, plans]);

  const accountOptions = useMemo(() => [
    { value: '', label: '不绑定账户' },
    ...accounts.map((account) => ({ value: String(account.id), label: account.name })),
  ], [accounts]);

  const openEdit = (plan: InvestmentPlanItem) => {
    setEditingPlan(plan);
    setForm({
      symbol: plan.symbol,
      market: plan.market,
      name: plan.name || '',
      accountId: plan.accountId ? String(plan.accountId) : '',
      strategyType: plan.strategyType,
      thesis: plan.thesis,
      invalidationNote: plan.invalidationNote,
      benchmarkSymbol: plan.benchmarkSymbol || '',
      plannedCapital: plan.plannedCapital == null ? '' : String(plan.plannedCapital),
      maxPositionPct: plan.maxPositionPct == null ? '' : String(plan.maxPositionPct),
      requiredCashPct: plan.requiredCashPct == null ? '' : String(plan.requiredCashPct),
      reviewDate: plan.reviewDate || '',
      notifyOnTrigger: plan.notifyOnTrigger,
      notificationChannel: plan.notificationChannels[0] || '',
      checkFrequency: plan.checkFrequency,
      steps: plan.steps.map((step) => ({
        key: String(step.id),
        action: step.action,
        metric: step.metric,
        operator: step.operator,
        threshold: String(step.threshold),
        upperThreshold: step.upperThreshold == null ? '' : String(step.upperThreshold),
        targetPositionPct: step.targetPositionPct == null ? '' : String(step.targetPositionPct),
        note: step.note || '',
      })),
    });
    setFormError(null);
    setDrawerOpen(true);
  };

  const updateFormStep = (key: string, fields: Partial<FormStep>) => {
    setForm((current) => ({
      ...current,
      steps: current.steps.map((step) => step.key === key ? { ...step, ...fields } : step),
    }));
  };

  const buildSteps = (): InvestmentPlanStepInput[] => form.steps.map((step, index) => {
    const threshold = toOptionalNumber(step.threshold);
    const upperThreshold = toOptionalNumber(step.upperThreshold);
    const targetPositionPct = toOptionalNumber(step.targetPositionPct);
    if (threshold == null || Number.isNaN(threshold)) {
      throw new Error(`第 ${index + 1} 档需要有效阈值`);
    }
    if (Number.isNaN(upperThreshold) || Number.isNaN(targetPositionPct)) {
      throw new Error(`第 ${index + 1} 档包含无效数字`);
    }
    if (step.operator === 'between' && upperThreshold == null) {
      throw new Error(`第 ${index + 1} 档需要填写区间上限`);
    }
    return {
      action: step.action,
      metric: step.metric,
      operator: step.operator,
      threshold,
      upperThreshold,
      targetPositionPct,
      note: step.note.trim() || null,
    };
  });

  const savePlan = async (activate: boolean) => {
    setFormError(null);
    const settingsOnly = Boolean(editingPlan && isSettingsOnlyPlan(editingPlan));
    const plannedCapital = toOptionalNumber(form.plannedCapital);
    const maxPositionPct = toOptionalNumber(form.maxPositionPct);
    const requiredCashPct = toOptionalNumber(form.requiredCashPct);
    if (!form.symbol.trim() || !form.thesis.trim() || !form.invalidationNote.trim()) {
      setFormError('请完整填写标的、投资逻辑和失效条件。');
      return;
    }
    if (Number.isNaN(plannedCapital) || Number.isNaN(maxPositionPct) || Number.isNaN(requiredCashPct)) {
      setFormError('计划资金、仓位和现金比例必须是有效数字。');
      return;
    }
    if (plannedCapital != null && plannedCapital <= 0) {
      setFormError('计划资金必须大于 0。');
      return;
    }
    let steps: InvestmentPlanStepInput[];
    try {
      steps = buildSteps();
    } catch (buildError) {
      setFormError(buildError instanceof Error ? buildError.message : '执行步骤无效');
      return;
    }
    if (activate && steps.length === 0) {
      setFormError('激活计划前至少需要一条执行步骤。');
      return;
    }

    setSaving(true);
    try {
      if (editingPlan) {
        const notificationSettings = {
          notifyOnTrigger: form.notifyOnTrigger,
          notificationChannels: form.notificationChannel ? [form.notificationChannel] : [],
          checkFrequency: form.checkFrequency,
        };
        await investmentPlansApi.update(editingPlan.id, settingsOnly ? {
          ...notificationSettings,
          ...(editingPlan.strategyType === 'index_crash'
            && editingPlan.executionSummary.completedExecutionCount === 0
            ? { plannedCapital }
            : {}),
        } : {
          ...notificationSettings,
          name: form.name.trim(),
          strategyType: form.strategyType,
          thesis: form.thesis.trim(),
          invalidationNote: form.invalidationNote.trim(),
          benchmarkSymbol: form.benchmarkSymbol.trim() || null,
          plannedCapital: form.strategyType === 'index_crash' ? plannedCapital : null,
          maxPositionPct,
          requiredCashPct,
          reviewDate: form.reviewDate || null,
          steps,
        });
        if (!settingsOnly && activate && editingPlan.status !== 'active') {
          await investmentPlansApi.setStatus(editingPlan.id, 'active');
        }
      } else {
        const payload: InvestmentPlanCreateRequest = {
          symbol: form.symbol.trim(),
          market: form.market,
          name: form.name.trim(),
          accountId: form.accountId ? Number(form.accountId) : null,
          strategyType: form.strategyType,
          status: activate ? 'active' : 'draft',
          thesis: form.thesis.trim(),
          invalidationNote: form.invalidationNote.trim(),
          benchmarkSymbol: form.benchmarkSymbol.trim() || null,
          plannedCapital: form.strategyType === 'index_crash' ? plannedCapital : null,
          maxPositionPct,
          requiredCashPct,
          reviewDate: form.reviewDate || null,
          notifyOnTrigger: form.notifyOnTrigger,
          notificationChannels: form.notificationChannel ? [form.notificationChannel] : [],
          checkFrequency: form.checkFrequency,
          steps,
        };
        await investmentPlansApi.create(payload);
      }
      setDrawerOpen(false);
      setNotice({
        variant: 'success',
        title: settingsOnly ? '检查与通知设置已保存' : activate ? '计划已进入执行' : '计划已保存',
        message: settingsOnly
          ? '新的检查频率与通知方式已生效。'
          : activate
          ? `系统将按“${CHECK_FREQUENCY_OPTIONS.find((item) => item.value === form.checkFrequency)?.label}”检查待执行条件。`
          : '草稿不会参与自动检查。',
      });
      await loadPlans();
    } catch (requestError) {
      setFormError(getParsedApiError(requestError).message);
    } finally {
      setSaving(false);
    }
  };

  const evaluateOne = async (plan: InvestmentPlanItem) => {
    if (uncertainEvaluations.has(plan.id)) return;
    setEvaluatingId(plan.id);
    setNotice(null);
    setError(null);
    let baselineLastEvaluatedAt: string | null;
    try {
      const latestPlan = await investmentPlansApi.get(plan.id);
      baselineLastEvaluatedAt = latestPlan.lastEvaluatedAt || null;
    } catch (requestError) {
      const parsedError = getParsedApiError(requestError);
      setError({
        ...parsedError,
        title: '无法开始检查',
        message: '无法读取计划的最新检查基线，本次未发起检查。请稍后重试。',
      });
      setEvaluatingId(null);
      return;
    }
    try {
      const response = await investmentPlansApi.evaluate(plan.id, plan.notifyOnTrigger);
      if (response.errors.length > 0 || response.plan.lastEvaluationStatus === 'data_missing') {
        setNotice({
          variant: 'warning',
          title: '行情数据不足',
          message: response.errors.length > 0
            ? `本次未完成条件校验：${response.errors.join('；')}`
            : '本次未完成条件校验，请稍后重试。',
        });
      } else {
        setNotice({
          variant: response.newlyTriggeredStepIds.length > 0 ? 'warning' : 'info',
          title: response.newlyTriggeredStepIds.length > 0 ? '发现新触发条件' : '检查完成',
          message: response.newlyTriggeredStepIds.length > 0
            ? `新触发 ${response.newlyTriggeredStepIds.length} 档${
              response.notification.sent
                ? '，通知已发送'
                : response.notification.queued
                  ? '，通知已进入后台发送'
                  : ''
            }，请核对行情和约束后人工处理。`
            : '当前没有新的待执行步骤。',
        });
      }
      setUncertainEvaluations((current) => {
        if (!current.has(plan.id)) return current;
        const next = new Map(current);
        next.delete(plan.id);
        return next;
      });
      await loadPlans();
    } catch (requestError) {
      const parsedError = getParsedApiError(requestError);
      if (parsedError.category === 'upstream_timeout' && parsedError.status == null) {
        const reconciled = await reconcileTimedOutEvaluation(plan.id, baselineLastEvaluatedAt);
        if (reconciled) {
          const statusLabel = reconciled.lastEvaluationStatus
            ? EVALUATION_STATUS_LABELS[reconciled.lastEvaluationStatus] || reconciled.lastEvaluationStatus
            : '已完成';
          setNotice({
            variant: 'warning',
            title: '检查已在后台完成',
            message: `请求等待超时，但已读取到新的检查结果：${statusLabel}。请查看执行轨道确认触发与通知状态。`,
          });
          setUncertainEvaluations((current) => {
            if (!current.has(plan.id)) return current;
            const next = new Map(current);
            next.delete(plan.id);
            return next;
          });
          await loadPlans();
        } else {
          setUncertainEvaluations((current) => new Map(current).set(plan.id, baselineLastEvaluatedAt));
          setError({
            ...parsedError,
            message: '请求等待超时，后台可能仍在处理。请勿重复检查，稍后点击“重新确认”读取最新状态。',
          });
        }
      } else {
        setError(parsedError);
      }
    } finally {
      setEvaluatingId(null);
    }
  };

  const confirmUncertainEvaluation = async (planId: number) => {
    if (!uncertainEvaluations.has(planId)) return;
    const baselineLastEvaluatedAt = uncertainEvaluations.get(planId) ?? null;
    setEvaluatingId(planId);
    setNotice(null);
    setError(null);
    try {
      const reconciled = await reconcileTimedOutEvaluation(planId, baselineLastEvaluatedAt);
      if (!reconciled) {
        setNotice({
          variant: 'warning',
          title: '检查仍在后台处理中',
          message: '尚未读取到新的检查时间，当前锁定保持不变；稍后可再次点击“重新确认”。',
        });
        return;
      }
      const statusLabel = reconciled.lastEvaluationStatus
        ? EVALUATION_STATUS_LABELS[reconciled.lastEvaluationStatus] || reconciled.lastEvaluationStatus
        : '已完成';
      setUncertainEvaluations((current) => {
        const next = new Map(current);
        next.delete(planId);
        return next;
      });
      setNotice({
        variant: 'warning',
        title: '后台检查已完成',
        message: `已读取到新的检查结果：${statusLabel}。检查按钮已恢复，请查看执行轨道确认触发与通知状态。`,
      });
      await loadPlans();
    } catch (requestError) {
      const parsedError = getParsedApiError(requestError);
      setError({
        ...parsedError,
        message: '重新确认失败，检查结果仍未知。当前锁定保持不变，请稍后重试。',
      });
    } finally {
      setEvaluatingId(null);
    }
  };

  const evaluateAll = async () => {
    if (uncertainEvaluations.size > 0) return;
    setEvaluatingId('all');
    setNotice(null);
    setError(null);
    try {
      const response = await investmentPlansApi.evaluateActive(false);
      const dataMissingCount = response.results.filter((result) => (
        result.errors.length > 0 || result.plan.lastEvaluationStatus === 'data_missing'
      )).length;
      setNotice({
        variant: dataMissingCount > 0 || response.triggered > 0 || response.errors.length > 0 ? 'warning' : 'success',
        title: dataMissingCount > 0 ? '部分计划数据不足' : '活跃计划检查完成',
        message: `已检查 ${response.evaluated} 份计划，新触发 ${response.triggered} 档，数据不足 ${dataMissingCount} 份，异常失败 ${response.errors.length} 份。`,
      });
      await loadPlans();
    } catch (requestError) {
      const parsedError = getParsedApiError(requestError);
      if (parsedError.category === 'upstream_timeout' && parsedError.status == null) {
        const refreshed = await loadPlans();
        setNotice(refreshed ? {
          variant: 'warning',
          title: '批量检查响应超时',
          message: '已刷新当前计划状态；后台可能仍在处理其余计划，请勿立即重复检查。',
        } : {
          variant: 'warning',
          title: '批量检查状态未知',
          message: '检查请求和状态刷新均未完成，后台可能仍在处理。请勿立即重复检查，稍后重新加载页面确认。',
        });
      } else {
        setError(parsedError);
      }
    } finally {
      setEvaluatingId(null);
    }
  };

  const transitionPlan = async (plan: InvestmentPlanItem, status: InvestmentPlanStatus) => {
    setTransitioningId(plan.id);
    try {
      await investmentPlansApi.setStatus(plan.id, status);
      setNotice({
        variant: status === 'active' ? 'success' : 'info',
        title: status === 'active' ? '计划已激活' : status === 'paused' ? '计划已暂停' : '计划已移除',
        message: status === 'active'
          ? '后续检查会评估这份计划。'
          : status === 'paused'
            ? '当前计划不会参与自动检查。'
            : '当前计划不会参与自动检查，可在“已移除”筛选中查看。',
      });
      await loadPlans();
    } catch (requestError) {
      setError(getParsedApiError(requestError));
    } finally {
      setTransitioningId(null);
      setClosePlan(null);
    }
  };

  const transitionStep = async (
    plan: InvestmentPlanItem,
    stepId: number,
    status: InvestmentPlanStepStatus,
  ) => {
    setTransitioningId(plan.id);
    try {
      await investmentPlansApi.setStepStatus(plan.id, stepId, status);
      await loadPlans();
    } catch (requestError) {
      setError(getParsedApiError(requestError));
    } finally {
      setTransitioningId(null);
    }
  };

  const openExecution = (plan: InvestmentPlanItem, step: InvestmentPlanStepItem) => {
    const price = plan.lastPrice && plan.lastPrice > 0 ? plan.lastPrice : null;
    const targetAmount = suggestedExecutionAmount(plan, step);
    const suggestedQuantity = price && targetAmount
      ? Math.floor(targetAmount / price / 100) * 100
      : null;
    setExecutionTarget({ plan, step });
    setExecutionForm({
      executionAt: currentDateTimeInputValue(),
      price: price == null ? '' : String(price),
      quantity: suggestedQuantity && suggestedQuantity > 0 ? String(suggestedQuantity) : '',
      fee: '0',
      note: '',
    });
    setExecutionError(null);
  };

  const saveExecution = async () => {
    if (!executionTarget) return;
    const price = Number(executionForm.price);
    const quantity = Number(executionForm.quantity);
    const fee = Number(executionForm.fee || '0');
    if (!executionForm.executionAt || !Number.isFinite(price) || price <= 0) {
      setExecutionError('请填写有效的实际成交时间和成交价格。');
      return;
    }
    if (!Number.isFinite(quantity) || quantity <= 0) {
      setExecutionError('成交份额必须大于 0。');
      return;
    }
    if (!Number.isFinite(fee) || fee < 0) {
      setExecutionError('手续费不能小于 0。');
      return;
    }
    const payload: InvestmentPlanExecutionRequest = {
      executionAt: executionForm.executionAt,
      price,
      quantity,
      fee,
      note: executionForm.note.trim() || null,
    };
    setSavingExecution(true);
    setExecutionError(null);
    try {
      await investmentPlansApi.recordStepExecution(
        executionTarget.plan.id,
        executionTarget.step.id,
        payload,
      );
      setExecutionTarget(null);
      setNotice({
        variant: 'success',
        title: '成交已登记',
        message: '本档已标记为已完成，持仓、剩余资金和收益复盘已更新。',
      });
      await loadPlans();
    } catch (requestError) {
      setExecutionError(getParsedApiError(requestError).message);
    } finally {
      setSavingExecution(false);
    }
  };

  return (
    <AppPage data-testid="investment-plans-page" className="max-w-[1680px] space-y-4">
      <section className="overflow-hidden rounded-lg border border-border bg-card shadow-sm">
        <div className="grid lg:grid-cols-[minmax(0,1.4fr)_minmax(420px,1fr)]">
          <div className="p-5 sm:p-6">
            <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <ClipboardList className="h-4 w-4" />
              先计划，再行动
            </div>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">投资策略计划</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
              把判断写成可执行条件。系统只检查价格、基准回撤和持仓纪律；触发后由你核对并决定，不会自动下单。
            </p>
            <div className="mt-5 flex flex-wrap gap-2">
              <Button onClick={() => openCreate()}>
                <Plus className="h-4 w-4" />
                制定计划
              </Button>
              <Button
                variant="outline"
                onClick={() => void evaluateAll()}
                isLoading={evaluatingId === 'all'}
                loadingText="检查中"
                disabled={uncertainEvaluations.size > 0}
              >
                <RefreshCw className="h-4 w-4" />
                检查活跃计划
              </Button>
            </div>
          </div>
          <div className="border-t border-border bg-muted/35 p-5 lg:border-l lg:border-t-0">
            <div className="grid h-full grid-cols-3 gap-3">
              {[
                ['01', '为什么投资', '写清事实和预期'],
                ['02', '如何执行', '把价格拆成档位'],
                ['03', '何时认错', '提前定义退出条件'],
              ].map(([number, title, description]) => (
                <div key={number} className="flex min-h-28 flex-col justify-between border-l border-border pl-3">
                  <span className="font-mono text-xs text-muted-foreground">{number}</span>
                  <div>
                    <div className="text-sm font-semibold text-foreground">{title}</div>
                    <div className="mt-1 text-xs leading-5 text-muted-foreground">{description}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <SummaryTile label="执行中" value={summary.active} icon={<Play className="h-4 w-4" />} tone="success" />
        <SummaryTile label="已触发" value={summary.triggered} icon={<BellRing className="h-4 w-4" />} tone="warning" />
        <SummaryTile label="纪律阻止" value={summary.blocked} icon={<ShieldCheck className="h-4 w-4" />} tone="warning" />
        <SummaryTile label="到期复查" value={summary.reviewDue} icon={<CalendarClock className="h-4 w-4" />} tone="info" />
        <SummaryTile label="数据不足" value={summary.dataMissing} icon={<AlertTriangle className="h-4 w-4" />} tone="danger" />
      </section>

      <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
        <div className="grid gap-3 md:grid-cols-[180px_180px_minmax(240px,1fr)]">
          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            options={STATUS_OPTIONS}
            label="状态"
          />
          <Select
            value={strategyFilter}
            onChange={setStrategyFilter}
            options={[{ value: '', label: '全部策略' }, ...STRATEGY_OPTIONS]}
            label="策略"
          />
          <Input
            label="查找计划"
            type="search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="标的代码、名称或投资逻辑"
          />
        </div>
      </section>

      {error ? <ApiErrorAlert error={error} /> : null}
      {notice ? <InlineAlert variant={notice.variant} title={notice.title} message={notice.message} /> : null}

      <section className="space-y-3">
        {loading ? (
          Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="h-64 animate-pulse rounded-lg border border-border bg-card" />
          ))
        ) : visiblePlans.length > 0 ? (
          visiblePlans.map((plan) => (
            <PlanCard
              key={plan.id}
              plan={plan}
              accountName={accounts.find((account) => account.id === plan.accountId)?.name}
              busy={transitioningId === plan.id || evaluatingId === plan.id}
              evaluationUncertain={uncertainEvaluations.has(plan.id)}
              onEvaluate={() => {
                if (uncertainEvaluations.has(plan.id)) void confirmUncertainEvaluation(plan.id);
                else void evaluateOne(plan);
              }}
              onEdit={() => openEdit(plan)}
              onStatus={(status) => {
                if (status === 'closed') setClosePlan(plan);
                else void transitionPlan(plan, status);
              }}
              onStepStatus={(stepId, status) => void transitionStep(plan, stepId, status)}
              onRecordExecution={(step) => openExecution(plan, step)}
            />
          ))
        ) : (
          <EmptyState
            title="还没有策略计划"
            description="从一只真正准备长期跟踪的股票开始，先写清买入理由、执行档位和失效条件。"
            icon={<ClipboardList className="h-6 w-6" />}
            action={<Button onClick={() => openCreate()}><Plus className="h-4 w-4" />制定第一份计划</Button>}
          />
        )}
      </section>

      <PlanEditorDrawer
        open={drawerOpen}
        editingPlan={editingPlan}
        settingsOnly={Boolean(editingPlan && isSettingsOnlyPlan(editingPlan))}
        form={form}
        accounts={accountOptions}
        error={formError}
        saving={saving}
        onClose={() => setDrawerOpen(false)}
        onFormChange={(fields) => setForm((current) => ({ ...current, ...fields }))}
        onStepChange={updateFormStep}
        onAddStep={() => setForm((current) => ({ ...current, steps: [...current.steps, newStep()] }))}
        onRemoveStep={(key) => setForm((current) => ({
          ...current,
          steps: current.steps.filter((step) => step.key !== key),
        }))}
        onSave={(activate) => void savePlan(activate)}
      />

      <ExecutionDrawer
        target={executionTarget}
        form={executionForm}
        error={executionError}
        saving={savingExecution}
        onClose={() => setExecutionTarget(null)}
        onFormChange={(fields) => setExecutionForm((current) => ({ ...current, ...fields }))}
        onSave={() => void saveExecution()}
      />

      <ConfirmDialog
        isOpen={Boolean(closePlan)}
        title="移除策略计划"
        message={`移除后，${closePlan?.name || closePlan?.symbol || '这份计划'} 将停止检查且不能重新激活，但计划及执行历史仍会保留。`}
        confirmText="确认移除"
        cancelText="取消"
        isDanger
        onConfirm={() => closePlan && void transitionPlan(closePlan, 'closed')}
        onCancel={() => setClosePlan(null)}
      />
    </AppPage>
  );
};

const SummaryTile: React.FC<{
  label: string;
  value: number;
  icon: React.ReactNode;
  tone: 'success' | 'warning' | 'info' | 'danger';
}> = ({ label, value, icon, tone }) => (
  <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
    <div className={cn(
      'flex items-center gap-2 text-xs font-medium',
      tone === 'success' && 'text-success',
      tone === 'warning' && 'text-warning',
      tone === 'info' && 'text-info',
      tone === 'danger' && 'text-danger',
    )}>
      {icon}
      {label}
    </div>
    <div className="mt-2 font-mono text-2xl font-semibold text-foreground">{value}</div>
  </div>
);

const MetricPair: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <div className="text-muted-foreground">{label}</div>
    <div className="mt-1 font-mono font-medium text-foreground">{value}</div>
  </div>
);

const ExecutionDrawer: React.FC<{
  target: ExecutionTarget | null;
  form: ExecutionForm;
  error: string | null;
  saving: boolean;
  onClose: () => void;
  onFormChange: (fields: Partial<ExecutionForm>) => void;
  onSave: () => void;
}> = ({ target, form, error, saving, onClose, onFormChange, onSave }) => {
  const price = Number(form.price);
  const quantity = Number(form.quantity);
  const amount = Number.isFinite(price) && price > 0 && Number.isFinite(quantity) && quantity > 0
    ? price * quantity
    : null;
  const suggestedAmount = target ? suggestedExecutionAmount(target.plan, target.step) : null;
  const currency = target ? MARKET_CURRENCY[target.plan.market] : 'CNY';
  const guidance = target?.plan.accountId != null
    ? '该计划已绑定账户，档位目标按账户总权益计算；请结合账户当前持仓人工填写，本页不自动建议金额。'
    : target && !target.plan.executionSummary.executionDataComplete
      ? '这是旧版成交补录，请按券商真实回报填写；补录完成前不自动建议金额。'
      : `建议新增投入约 ${currency} ${formatNumber(suggestedAmount)}。`;

  return (
    <Drawer
      isOpen={Boolean(target)}
      onClose={onClose}
      title="登记实际买入"
      width="max-w-xl"
      zIndex={60}
    >
      {target ? (
        <div className="space-y-5">
          <InlineAlert
            variant="info"
            title={`${target.plan.name || target.plan.symbol} · ${actionLabel(target.step.action)}`}
            message={`本档目标仓位 ${formatNumber(target.step.targetPositionPct)}%。${guidance} 请按真实成交回报填写，系统不会代替券商下单。`}
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Input
              label="实际成交时间"
              type="datetime-local"
              step="1"
              max={currentDateTimeInputValue()}
              value={form.executionAt}
              onChange={(event) => onFormChange({ executionAt: event.target.value })}
            />
            <Input
              label="成交价格"
              type="number"
              min="0"
              step="any"
              value={form.price}
              onChange={(event) => onFormChange({ price: event.target.value })}
            />
            <Input
              label="成交份额"
              type="number"
              min="0"
              step="any"
              value={form.quantity}
              onChange={(event) => onFormChange({ quantity: event.target.value })}
              hint="A 股 ETF 通常按 100 份整数倍成交，请以实际回报为准。"
            />
            <Input
              label={`手续费（${currency}）`}
              type="number"
              min="0"
              step="any"
              value={form.fee}
              onChange={(event) => onFormChange({ fee: event.target.value })}
            />
          </div>
          <div className="rounded-md border border-border bg-muted/20 p-3 text-sm">
            <span className="text-muted-foreground">成交金额：</span>
            <strong className="font-mono text-foreground">{formatMoney(amount, target.plan.market)}</strong>
          </div>
          <Input
            label="成交备注"
            value={form.note}
            maxLength={255}
            onChange={(event) => onFormChange({ note: event.target.value })}
            placeholder="可填写券商流水号或执行说明"
          />
          {error ? <InlineAlert variant="danger" title="无法登记成交" message={error} /> : null}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={onSave} isLoading={saving} loadingText="登记中">
              <Check className="h-4 w-4" />确认登记
            </Button>
          </div>
        </div>
      ) : null}
    </Drawer>
  );
};

const PlanCard: React.FC<{
  plan: InvestmentPlanItem;
  accountName?: string;
  busy: boolean;
  evaluationUncertain: boolean;
  onEvaluate: () => void;
  onEdit: () => void;
  onStatus: (status: InvestmentPlanStatus) => void;
  onStepStatus: (stepId: number, status: InvestmentPlanStepStatus) => void;
  onRecordExecution: (step: InvestmentPlanStepItem) => void;
}> = ({ plan, accountName, busy, evaluationUncertain, onEvaluate, onEdit, onStatus, onStepStatus, onRecordExecution }) => {
  const statusMeta = STATUS_META[plan.status];
  const frequencyLabel = CHECK_FREQUENCY_OPTIONS.find((item) => item.value === plan.checkFrequency)?.label || plan.checkFrequency;
  const selectedChannel = plan.notificationChannels[0] || '';
  const notificationLabel = plan.notifyOnTrigger
    ? NOTIFICATION_CHANNEL_OPTIONS.find((item) => item.value === selectedChannel)?.label || selectedChannel
    : '关闭';
  return (
    <article className={cn(
      'rounded-lg border bg-card shadow-sm',
      plan.triggeredStepCount > 0 ? 'border-warning/45' : 'border-border',
    )}>
      <div className="flex flex-col gap-4 border-b border-border p-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-foreground">{plan.name || plan.symbol}</h2>
            <span className="font-mono text-xs text-muted-foreground">{plan.symbol}</span>
            <Badge variant="info">{plan.strategyLabel}</Badge>
            <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
            {plan.reviewDue ? <Badge variant="warning">到期复查</Badge> : null}
            {plan.triggeredStepCount > 0 ? <Badge variant="warning">{plan.triggeredStepCount} 档待处理</Badge> : null}
          </div>
          <div className="mt-3 grid gap-x-6 gap-y-2 text-xs text-muted-foreground sm:grid-cols-2 lg:grid-cols-4">
            <span>当前价 <strong className="font-mono text-foreground">{formatNumber(plan.lastPrice, 4)}</strong></span>
            <span>最大仓位 <strong className="text-foreground">{plan.maxPositionPct == null ? '--' : `${formatNumber(plan.maxPositionPct)}%`}</strong></span>
            <span>现金底线 <strong className="text-foreground">{plan.requiredCashPct == null ? '--' : `${formatNumber(plan.requiredCashPct)}%`}</strong></span>
            <span>账户 <strong className="text-foreground">{accountName || '未绑定'}</strong></span>
            <span>计价 <strong className="text-foreground">{MARKET_CURRENCY[plan.market]}（市场本币）</strong></span>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <span>自动检查 <strong className="text-foreground">{frequencyLabel}</strong></span>
            <span>触发通知 <strong className="text-foreground">{notificationLabel}</strong></span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {plan.status === 'active' ? (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={onEvaluate}
                isLoading={busy}
                loadingText="检查中"
              >
                <RefreshCw className="h-4 w-4" />{evaluationUncertain ? '重新确认' : '检查'}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onStatus('paused')} disabled={busy}>
                <CirclePause className="h-4 w-4" />暂停
              </Button>
            </>
          ) : plan.status !== 'closed' ? (
            <Button size="sm" variant="secondary" onClick={() => onStatus('active')} disabled={busy}>
              <Play className="h-4 w-4" />激活
            </Button>
          ) : null}
          {plan.status !== 'closed' ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={onEdit}
              disabled={busy}
            >
              <Pencil className="h-4 w-4" />{isSettingsOnlyPlan(plan) ? '设置' : '编辑'}
            </Button>
          ) : null}
          {plan.status !== 'closed' ? (
            <Button size="sm" variant="danger-subtle" onClick={() => onStatus('closed')} disabled={busy}>
              <X className="h-4 w-4" />移除
            </Button>
          ) : null}
        </div>
      </div>

      <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_minmax(420px,0.9fr)]">
        <div className="space-y-4 p-4 lg:border-r lg:border-border">
          <div>
            <div className="text-xs font-medium text-muted-foreground">投资逻辑</div>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-foreground">{plan.thesis}</p>
          </div>
          <div className="rounded-md border border-danger/20 bg-danger/5 p-3">
            <div className="flex items-center gap-2 text-xs font-medium text-danger">
              <AlertTriangle className="h-4 w-4" />
              失效条件
            </div>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-foreground">{plan.invalidationNote}</p>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <span>最近检查：{formatDateTime(plan.lastEvaluatedAt)}</span>
            <span>检查结果：{plan.lastEvaluationStatus ? (EVALUATION_STATUS_LABELS[plan.lastEvaluationStatus] || plan.lastEvaluationStatus) : '--'}</span>
            {plan.reviewDate ? <span>复查日期：{plan.reviewDate}</span> : null}
          </div>
          {plan.lastEvaluationNote ? (
            <div className="text-xs leading-5 text-muted-foreground">{plan.lastEvaluationNote}</div>
          ) : null}
          {plan.strategyType === 'index_crash' ? (
            plan.plannedCapital == null ? (
              <InlineAlert
                variant="warning"
                title={plan.executionSummary.unrecordedCompletedCount > 0 ? '旧版成交记录待补录' : '尚未设置计划资金'}
                message={plan.executionSummary.unrecordedCompletedCount > 0
                  ? `有 ${plan.executionSummary.unrecordedCompletedCount} 个旧版已完成档位需要补录。可补录价格、份额和费用，但未设置计划资金时不会计算剩余现金和计划资金占用。`
                  : '在“设置”中填写计划资金后，才能登记实际买入并核算持仓与剩余现金。'}
              />
            ) : (
              <div className="rounded-lg border border-border bg-muted/20 p-3">
                <div className="text-xs font-semibold text-foreground">执行复盘</div>
                {!plan.executionSummary.executionDataComplete ? (
                  <div className="mt-2 text-xs leading-5 text-warning">
                    有 {plan.executionSummary.unrecordedCompletedCount} 个旧版已完成档位尚未补录成交；补录完成前不会计算余额、收益或仓位，也不能登记新触发档位。
                  </div>
                ) : null}
                <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-xs sm:grid-cols-4">
                  <MetricPair label="计划资金" value={formatMoney(plan.plannedCapital, plan.market)} />
                  <MetricPair label="累计投入" value={formatMoney(plan.executionSummary.totalCost, plan.market)} />
                  <MetricPair label="剩余现金" value={formatMoney(plan.executionSummary.remainingCash, plan.market)} />
                  <MetricPair label="持有份额" value={formatNumber(plan.executionSummary.totalQuantity, 4)} />
                  <MetricPair label="平均成本" value={formatNumber(plan.executionSummary.averageCost, 4)} />
                  <MetricPair
                    label="计划资金占用"
                    value={`${formatNumber(plan.executionSummary.capitalUtilizationPct)}%`}
                  />
                  <MetricPair
                    label={plan.accountId == null ? '档位目标 / 偏差' : '账户目标仓位（独立口径）'}
                    value={plan.accountId == null
                      ? `${formatNumber(plan.executionSummary.targetPositionPct)}% / ${formatNumber(plan.executionSummary.targetDeviationPct)}%`
                      : `${formatNumber(plan.executionSummary.targetPositionPct)}%`}
                  />
                  <MetricPair label="浮动盈亏" value={formatMoney(plan.executionSummary.unrealizedPnl, plan.market)} />
                  <MetricPair label="实际收益" value={`${formatNumber(plan.executionSummary.returnPct)}%`} />
                </div>
              </div>
            )
          ) : null}
        </div>

        <div className="p-4">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold text-foreground">执行轨道</div>
              <div className="mt-1 text-xs text-muted-foreground">触发只代表达到预设条件，不代表必须交易。</div>
            </div>
            <Badge variant="default">{plan.steps.length} 档</Badge>
          </div>
          <div className="relative space-y-0 before:absolute before:bottom-4 before:left-[7px] before:top-4 before:w-px before:bg-border">
            {plan.steps.map((step) => {
              const meta = STEP_STATUS_META[step.status];
              const isEtfBuyStep = plan.strategyType === 'index_crash' && ['buy', 'add'].includes(step.action);
              const needsLegacyBackfill = isEtfBuyStep && step.status === 'completed' && step.executionAmount == null;
              return (
                <div key={step.id} className="relative grid grid-cols-[16px_minmax(0,1fr)] gap-3 py-3">
                  <span className={cn('relative z-10 mt-1 h-4 w-4 rounded-full border-2', meta.dot)} />
                  <div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-foreground">{actionLabel(step.action)}</span>
                        <span className="font-mono text-xs text-muted-foreground">{conditionText(step)}</span>
                        <span className={cn('text-xs font-medium', meta.tone)}>{meta.label}</span>
                      </div>
                      {(plan.status !== 'closed' && step.status === 'triggered') || needsLegacyBackfill ? (
                        <div className="flex gap-1">
                          {isEtfBuyStep ? (
                            <Button size="xsm" variant="secondary" onClick={() => onRecordExecution(step)} disabled={busy || (!needsLegacyBackfill && plan.plannedCapital == null)}>
                              <Check className="h-3.5 w-3.5" />{needsLegacyBackfill ? '补录成交' : plan.plannedCapital == null ? '先设置资金' : '登记买入'}
                            </Button>
                          ) : (
                            <Button size="xsm" variant="secondary" onClick={() => onStepStatus(step.id, 'completed')} disabled={busy}>
                              <Check className="h-3.5 w-3.5" />完成
                            </Button>
                          )}
                          {step.status === 'triggered' ? (
                            <>
                              <Button size="xsm" variant="ghost" onClick={() => onStepStatus(step.id, 'skipped')} disabled={busy}>
                                <SkipForward className="h-3.5 w-3.5" />跳过
                              </Button>
                              <Button size="xsm" variant="ghost" onClick={() => onStepStatus(step.id, 'pending')} disabled={busy}>
                                <RotateCcw className="h-3.5 w-3.5" />重置
                              </Button>
                            </>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                      {step.targetPositionPct != null ? <span>目标仓位 {formatNumber(step.targetPositionPct)}%</span> : null}
                      {step.note ? <span>{step.note}</span> : null}
                      {step.triggeredAt ? <span>触发于 {formatDateTime(step.triggeredAt)}</span> : null}
                      {step.executionAt ? <span>成交于 {formatDateTime(step.executionAt)}</span> : step.executionDate ? <span>成交日 {step.executionDate}</span> : null}
                      {step.executionPrice != null ? <span>成交价 {formatNumber(step.executionPrice, 4)}</span> : null}
                      {step.executionQuantity != null ? <span>份额 {formatNumber(step.executionQuantity, 4)}</span> : null}
                      {step.executionAmount != null ? <span>金额 {formatMoney(step.executionAmount, plan.market)}</span> : null}
                      {step.executionFee != null ? <span>手续费 {formatMoney(step.executionFee, plan.market)}</span> : null}
                      {step.executionNote ? <span>{step.executionNote}</span> : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </article>
  );
};

const PlanEditorDrawer: React.FC<{
  open: boolean;
  editingPlan: InvestmentPlanItem | null;
  settingsOnly: boolean;
  form: PlanForm;
  accounts: Array<{ value: string; label: string }>;
  error: string | null;
  saving: boolean;
  onClose: () => void;
  onFormChange: (fields: Partial<PlanForm>) => void;
  onStepChange: (key: string, fields: Partial<FormStep>) => void;
  onAddStep: () => void;
  onRemoveStep: (key: string) => void;
  onSave: (activate: boolean) => void;
}> = ({
  open,
  editingPlan,
  settingsOnly,
  form,
  accounts,
  error,
  saving,
  onClose,
  onFormChange,
  onStepChange,
  onAddStep,
  onRemoveStep,
  onSave,
}) => (
  <Drawer isOpen={open} onClose={onClose} title={settingsOnly ? '检查与通知设置' : editingPlan ? '编辑策略计划' : '制定策略计划'} width="max-w-4xl">
    <div className="space-y-6">
      {settingsOnly ? (
        <>
          <InlineAlert
            variant="info"
            title="执行条件保持不变"
            message={form.strategyType === 'index_crash'
              ? '这份计划正在执行或已有执行历史，本次只修改执行资金、检查频率和通知方式。'
              : '这份计划正在执行或已有执行历史，本次只修改检查频率和通知方式。'}
          />
          {form.strategyType === 'index_crash' ? (
            <Input
              label="计划资金"
              type="number"
              min="0"
              step="any"
              value={form.plannedCapital}
              onChange={(event) => onFormChange({ plannedCapital: event.target.value })}
              disabled={Boolean(editingPlan?.executionSummary.completedExecutionCount)}
              hint={editingPlan?.executionSummary.completedExecutionCount
                ? `已有成交记录，计划资金不能再修改；仍按 ${MARKET_CURRENCY[form.market]} 市场本币计价。`
                : `按 ${MARKET_CURRENCY[form.market]} 市场本币计价，用于计算每档建议投入、剩余现金和实际仓位；不要求绑定账户。`}
            />
          ) : null}
        </>
      ) : null}

      <section className={settingsOnly ? 'hidden' : undefined}>
        <div className="mb-3 flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">01</span>
          <h3 className="text-sm font-semibold text-foreground">标的与投资逻辑</h3>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Input label="标的代码" value={form.symbol} onChange={(event) => onFormChange({ symbol: event.target.value })} disabled={Boolean(editingPlan)} />
          <Input label="标的名称" value={form.name} onChange={(event) => onFormChange({ name: event.target.value })} />
          <Select
            label="市场"
            value={form.market}
            onChange={(value) => {
              const market = value as PlanForm['market'];
              onFormChange({
                market,
                ...(market === 'us' && form.checkFrequency === 'minute' ? { checkFrequency: 'daily' } : {}),
              });
            }}
            options={MARKET_OPTIONS}
            disabled={Boolean(editingPlan)}
          />
          <Select
            label="策略类型"
            value={form.strategyType}
            onChange={(value) => {
              const strategyType = value as InvestmentStrategyType;
              onFormChange(editingPlan ? { strategyType } : strategyTemplate(strategyType));
            }}
            options={STRATEGY_OPTIONS}
          />
          <Select label="关联账户" value={form.accountId} onChange={(value) => onFormChange({ accountId: value })} options={accounts} disabled={Boolean(editingPlan)} />
          <Input label="复查日期" type="date" value={form.reviewDate} onChange={(event) => onFormChange({ reviewDate: event.target.value })} />
        </div>
        <div className="mt-4 grid gap-4">
          <TextAreaField label="为什么投资" value={form.thesis} onChange={(value) => onFormChange({ thesis: value })} placeholder="只写能被事实验证的判断：竞争力、现金流、估值、行业位置……" />
          <TextAreaField label="什么情况下认错" value={form.invalidationNote} onChange={(value) => onFormChange({ invalidationNote: value })} placeholder="提前写下会推翻原判断的事实，而不是价格波动本身。" tone="danger" />
        </div>
      </section>

      <section className={cn('border-t border-border pt-6', settingsOnly && 'hidden')}>
        <div className="mb-3 flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">02</span>
          <h3 className="text-sm font-semibold text-foreground">账户纪律</h3>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {form.strategyType === 'index_crash' ? (
            <Input label="计划资金" type="number" min="0" step="any" value={form.plannedCapital} onChange={(event) => onFormChange({ plannedCapital: event.target.value })} hint={`按 ${MARKET_CURRENCY[form.market]} 市场本币计价，用于人工成交与复盘，可不绑定账户`} />
          ) : null}
          <Input label="最大仓位 %" type="number" min="0" max="100" value={form.maxPositionPct} onChange={(event) => onFormChange({ maxPositionPct: event.target.value })} />
          <Input label="最低现金 %" type="number" min="0" max="100" value={form.requiredCashPct} onChange={(event) => onFormChange({ requiredCashPct: event.target.value })} />
          <Input label="对标指数" value={form.benchmarkSymbol} onChange={(event) => onFormChange({ benchmarkSymbol: event.target.value })} hint="使用基准回撤条件时必填" />
        </div>
      </section>

      <section className="border-t border-border pt-6">
        <div className="mb-3 flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">03</span>
          <h3 className="text-sm font-semibold text-foreground">检查与通知</h3>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Select
              label="自动检查频率"
              value={form.checkFrequency}
              onChange={(value) => onFormChange({ checkFrequency: value as InvestmentPlanCheckFrequency })}
              options={form.market === 'us'
                ? CHECK_FREQUENCY_OPTIONS.filter((item) => item.value !== 'minute')
                : CHECK_FREQUENCY_OPTIONS}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              自动检查需持续运行 schedule 服务；盘中高频仅支持 A 股/港股，并只在对应市场交易时段执行。
            </p>
          </div>
          <div>
            <Select
              label="通知渠道"
              value={form.notificationChannel}
              onChange={(value) => onFormChange({ notificationChannel: value as PlanForm['notificationChannel'] })}
              options={NOTIFICATION_CHANNEL_OPTIONS}
              disabled={!form.notifyOnTrigger}
            />
            <p className="mt-1 text-xs text-muted-foreground">指定渠道前，请先在设置页完成对应渠道配置。</p>
          </div>
        </div>
        <Checkbox
          containerClassName="mt-4"
          label="达到条件时发送通知"
          checked={form.notifyOnTrigger}
          onChange={(event) => onFormChange({ notifyOnTrigger: event.target.checked })}
        />
      </section>

      <section className={cn('border-t border-border pt-6', settingsOnly && 'hidden')}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">04</span>
            <h3 className="text-sm font-semibold text-foreground">执行档位</h3>
          </div>
          <Button size="sm" variant="outline" onClick={onAddStep}><Plus className="h-4 w-4" />增加一档</Button>
        </div>
        <div className="space-y-3">
          {form.steps.map((step, index) => (
            <div key={step.key} className="rounded-lg border border-border bg-muted/20 p-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="font-mono text-xs text-muted-foreground">档位 {String(index + 1).padStart(2, '0')}</span>
                <Button size="xsm" variant="ghost" onClick={() => onRemoveStep(step.key)} aria-label={`删除第 ${index + 1} 档`}>
                  <Trash2 className="h-4 w-4 text-danger" />
                </Button>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <Select value={step.action} onChange={(value) => onStepChange(step.key, { action: value as InvestmentPlanStepAction })} options={ACTION_OPTIONS} label="动作" />
                <Select value={step.metric} onChange={(value) => onStepChange(step.key, { metric: value as InvestmentPlanStepMetric })} options={METRIC_OPTIONS} label="检查指标" />
                <Select value={step.operator} onChange={(value) => onStepChange(step.key, { operator: value as InvestmentPlanStepOperator })} options={OPERATOR_OPTIONS} label="条件" />
                <Input label={step.metric === 'price' ? '价格阈值' : '回撤阈值 %'} type="number" min="0" step="any" value={step.threshold} onChange={(event) => onStepChange(step.key, { threshold: event.target.value })} />
                {step.operator === 'between' ? (
                  <Input label="区间上限" type="number" min="0" step="any" value={step.upperThreshold} onChange={(event) => onStepChange(step.key, { upperThreshold: event.target.value })} />
                ) : null}
                <Input label="执行后目标仓位 %" type="number" min="0" max="100" step="any" value={step.targetPositionPct} onChange={(event) => onStepChange(step.key, { targetPositionPct: event.target.value })} />
                <div className="sm:col-span-2 lg:col-span-3">
                  <Input label="本档备注" value={step.note} onChange={(event) => onStepChange(step.key, { note: event.target.value })} placeholder="例如：只使用第一笔闲置资金，成交前复核公告" />
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {error ? <InlineAlert variant="danger" title="无法保存计划" message={error} /> : null}

      <div className="sticky bottom-0 flex flex-wrap justify-end gap-2 border-t border-border bg-card py-4">
        <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
        {settingsOnly ? (
          <Button onClick={() => onSave(false)} isLoading={saving} loadingText="保存中">保存设置</Button>
        ) : (
          <>
            <Button variant="outline" onClick={() => onSave(false)} isLoading={saving} loadingText="保存中">保存草稿</Button>
            <Button onClick={() => onSave(true)} isLoading={saving} loadingText="激活中">
              <Play className="h-4 w-4" />保存并激活
            </Button>
          </>
        )}
      </div>
    </div>
  </Drawer>
);

const TextAreaField: React.FC<{
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  tone?: 'default' | 'danger';
}> = ({ label, value, onChange, placeholder, tone = 'default' }) => (
  <label className="block">
    <span className={cn('mb-2 block text-sm font-medium', tone === 'danger' ? 'text-danger' : 'text-foreground')}>{label}</span>
    <textarea
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      rows={4}
      className={cn(
        'w-full resize-y rounded-md border bg-background px-3 py-2 text-sm leading-6 text-foreground shadow-sm outline-none transition-colors placeholder:text-muted-foreground focus:ring-2 focus:ring-ring',
        tone === 'danger' ? 'border-danger/30' : 'border-input',
      )}
    />
  </label>
);

export default InvestmentPlansPage;
