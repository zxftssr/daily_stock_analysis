import React, { Fragment, useState } from 'react';
import {
  BarChart3,
  BriefcaseBusiness,
  Compass,
  Home,
  Lightbulb,
  ListChecks,
  LogOut,
  MessageSquareQuote,
  PanelLeftClose,
  PanelLeftOpen,
  Settings2,
} from 'lucide-react';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { useAgentChatStore } from '../../stores/agentChatStore';
import { cn } from '../../utils/cn';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { StatusDot } from '../common/StatusDot';
import { Tooltip } from '../common/Tooltip';
import { ThemeToggle } from '../theme/ThemeToggle';

type SidebarNavProps = {
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onNavigate?: () => void;
};

type NavItem = {
  key: string;
  label: string;
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  exact?: boolean;
  badge?: 'completion';
};

const NAV_SECTIONS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: '主要功能',
    items: [
      { key: 'home', label: '首页', to: '/', icon: Home, exact: true },
      { key: 'discover', label: '发现', to: '/discover', icon: Compass },
      { key: 'candidates', label: '候选', to: '/candidates', icon: Lightbulb },
    ],
  },
  {
    label: '研究工具',
    items: [
      { key: 'chat', label: '问股', to: '/chat', icon: MessageSquareQuote, badge: 'completion' },
      { key: 'portfolio', label: '持仓', to: '/portfolio', icon: BriefcaseBusiness },
      { key: 'plans', label: '策略计划', to: '/plans', icon: ListChecks },
      { key: 'backtest', label: '回测', to: '/backtest', icon: BarChart3 },
    ],
  },
  {
    label: '系统',
    items: [
      { key: 'settings', label: '设置', to: '/settings', icon: Settings2 },
    ],
  },
];

export const SidebarNav: React.FC<SidebarNavProps> = ({
  collapsed = false,
  onToggleCollapsed,
  onNavigate,
}) => {
  const { authEnabled, logout } = useAuth();
  const completionBadge = useAgentChatStore((state) => state.completionBadge);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const withCollapsedHelp = (content: string, control: React.ReactNode) => (
    collapsed ? (
      <Tooltip content={content} side="right" className="w-full">
        {control}
      </Tooltip>
    ) : control
  );

  return (
    <div className="flex h-full w-full flex-col p-3">
      <div className={cn('flex h-11 items-center gap-3 px-2', collapsed && 'justify-center px-0')}>
        <div className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-primary text-[10px] font-bold text-primary-foreground">
          DSA
        </div>
        {!collapsed ? (
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-foreground">每日投研</p>
            <p className="truncate text-xs text-muted-foreground">股票分析工作台</p>
          </div>
        ) : null}
      </div>

      <nav className="mt-4 min-h-0 flex-1 overflow-y-auto" aria-label="主导航">
        {NAV_SECTIONS.map((section, sectionIndex) => (
          <Fragment key={section.label}>
            {!collapsed ? (
              <p className={cn('px-3 pb-1 text-xs font-medium text-muted-foreground', sectionIndex > 0 && 'mt-5')}>
                {section.label}
              </p>
            ) : sectionIndex > 0 ? <div className="my-3 border-t border-border" /> : null}

            <div className="space-y-1">
              {section.items.map(({ key, label, to, icon: Icon, exact, badge }) => {
                const navLink = (
                  <NavLink
                    to={to}
                    end={exact}
                    onClick={onNavigate}
                    aria-label={label}
                    className={({ isActive }) => cn(
                      'relative flex h-10 items-center gap-3 rounded-md px-3 text-sm transition-colors duration-200',
                      collapsed && 'justify-center px-0',
                      isActive
                        ? 'bg-accent font-medium text-accent-foreground'
                        : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                    )}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {!collapsed ? <span className="truncate">{label}</span> : null}
                    {badge === 'completion' && completionBadge ? (
                      <StatusDot
                        tone="info"
                        data-testid="chat-completion-badge"
                        className={cn('absolute right-3', collapsed && 'right-2 top-2')}
                        aria-label="问股有新消息"
                      />
                    ) : null}
                  </NavLink>
                );

                return collapsed ? (
                  <Tooltip key={key} content={label} side="right" className="w-full">
                    {navLink}
                  </Tooltip>
                ) : (
                  <Fragment key={key}>{navLink}</Fragment>
                );
              })}
            </div>
          </Fragment>
        ))}
      </nav>

      <div className="space-y-1 border-t border-border pt-3">
        <ThemeToggle variant="nav" collapsed={collapsed} />

        {authEnabled ? withCollapsedHelp('退出', (
          <button
            type="button"
            aria-label="退出"
            onClick={() => setShowLogoutConfirm(true)}
            className={cn(
              'flex h-10 w-full cursor-pointer items-center gap-3 rounded-md px-3 text-sm text-muted-foreground transition-colors duration-200 hover:bg-accent hover:text-accent-foreground',
              collapsed && 'justify-center px-0',
            )}
          >
            <LogOut className="h-4 w-4 shrink-0" />
            {!collapsed ? <span>退出</span> : null}
          </button>
        )) : null}

        {onToggleCollapsed ? withCollapsedHelp('展开侧边栏', (
          <button
            type="button"
            aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
            onClick={onToggleCollapsed}
            className={cn(
              'flex h-10 w-full cursor-pointer items-center gap-3 rounded-md px-3 text-sm text-muted-foreground transition-colors duration-200 hover:bg-accent hover:text-accent-foreground',
              collapsed && 'justify-center px-0',
            )}
          >
            {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
            {!collapsed ? <span>折叠侧边栏</span> : null}
          </button>
        )) : null}
      </div>

      <ConfirmDialog
        isOpen={showLogoutConfirm}
        title="退出登录"
        message="确认退出当前登录状态吗？退出后需要重新输入密码。"
        confirmText="确认退出"
        cancelText="取消"
        isDanger
        onConfirm={() => {
          setShowLogoutConfirm(false);
          onNavigate?.();
          void logout();
        }}
        onCancel={() => setShowLogoutConfirm(false)}
      />
    </div>
  );
};
