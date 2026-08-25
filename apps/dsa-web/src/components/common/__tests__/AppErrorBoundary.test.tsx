import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppErrorBoundary } from '../AppErrorBoundary';

const BrokenChild = () => {
  throw new Error('render failed');
};

describe('AppErrorBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders children while the app is healthy', () => {
    render(
      <AppErrorBoundary>
        <p>正常页面</p>
      </AppErrorBoundary>,
    );

    expect(screen.getByText('正常页面')).toBeInTheDocument();
  });

  it('shows a recoverable fallback when rendering fails', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    render(
      <AppErrorBoundary>
        <BrokenChild />
      </AppErrorBoundary>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('页面未能正常更新');
    expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument();
    expect(screen.getByText(/关闭当前站点的自动翻译/)).toBeInTheDocument();
  });
});
