import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { SidebarNav } from '../SidebarNav';

const mockLogout = vi.fn().mockResolvedValue(undefined);
const mockThemeToggle = vi.fn(({ collapsed }: { collapsed?: boolean }) => (
  <button type="button">{collapsed ? '切换主题(折叠)' : '切换主题'}</button>
));

const completionBadgeState = { value: true };

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: true,
    logout: mockLogout,
  }),
}));

vi.mock('../../../stores/agentChatStore', () => ({
  useAgentChatStore: (selector: (state: { completionBadge: boolean }) => unknown) =>
    selector({ completionBadge: completionBadgeState.value }),
}));

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: (props: { collapsed?: boolean }) => mockThemeToggle(props),
}));

describe('SidebarNav', () => {
  it('groups routes and exposes an accessible collapse control', () => {
    const onToggleCollapsed = vi.fn();
    render(
      <MemoryRouter initialEntries={['/discover']}>
        <SidebarNav onToggleCollapsed={onToggleCollapsed} />
      </MemoryRouter>,
    );

    expect(screen.getByText('主要功能')).toBeInTheDocument();
    expect(screen.getByText('研究工具')).toBeInTheDocument();
    expect(screen.getByText('系统')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '折叠侧边栏' }));
    expect(onToggleCollapsed).toHaveBeenCalledTimes(1);
  });

  it('renders the candidate pool navigation entry', () => {
    render(
      <MemoryRouter initialEntries={['/candidates']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '候选' })).toHaveAttribute('href', '/candidates');
  });

  it('renders the investment plan navigation entry', () => {
    render(
      <MemoryRouter initialEntries={['/plans']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '策略计划' })).toHaveAttribute('href', '/plans');
  });

  it('shows the shared completion badge only when chat completion is pending', () => {
    completionBadgeState.value = true;

    const { rerender } = render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('chat-completion-badge')).toBeInTheDocument();
    expect(screen.getByLabelText('问股有新消息')).toBeInTheDocument();

    completionBadgeState.value = false;
    rerender(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId('chat-completion-badge')).not.toBeInTheDocument();
  });

  it('renders the collapsed theme toggle variant when the sidebar is collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav collapsed />
      </MemoryRouter>,
    );

    expect(mockThemeToggle).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'nav', collapsed: true }),
    );
    expect(screen.getByRole('button', { name: '切换主题(折叠)' })).toBeInTheDocument();
  });

  it('shows visible route help on hover and keyboard focus when collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav collapsed />
      </MemoryRouter>,
    );

    const homeLink = screen.getByRole('link', { name: '首页' });
    fireEvent.mouseEnter(homeLink);
    expect(screen.getByRole('tooltip')).toHaveTextContent('首页');
    expect(screen.getByRole('tooltip')).toHaveClass('origin-left');

    fireEvent.mouseLeave(homeLink);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    fireEvent.focus(homeLink);
    expect(screen.getByRole('tooltip')).toHaveTextContent('首页');
  });

  it('shows visible help for collapsed logout and expand controls', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav collapsed onToggleCollapsed={vi.fn()} />
      </MemoryRouter>,
    );

    const logoutButton = screen.getByRole('button', { name: '退出' });
    fireEvent.mouseEnter(logoutButton);
    expect(screen.getByRole('tooltip')).toHaveTextContent('退出');
    fireEvent.mouseLeave(logoutButton);

    const expandButton = screen.getByRole('button', { name: '展开侧边栏' });
    fireEvent.focus(expandButton);
    expect(screen.getByRole('tooltip')).toHaveTextContent('展开侧边栏');
  });

  it('opens the logout confirmation and confirms logout', async () => {
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '退出' }));

    expect(await screen.findByRole('heading', { name: '退出登录' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认退出' }));
    expect(mockLogout).toHaveBeenCalled();
  });
});
