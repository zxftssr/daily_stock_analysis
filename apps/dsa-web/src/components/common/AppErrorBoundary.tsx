import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from './Button';

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  hasError: boolean;
}

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('DSA page rendering failed', error, info);
  }

  private handleReload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <main
          className="flex min-h-screen items-center justify-center bg-base px-4 text-foreground"
          role="alert"
        >
          <section className="w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-sm">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              页面渲染异常
            </p>
            <h1 className="mt-2 text-xl font-semibold">页面未能正常更新</h1>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              浏览器翻译或扩展可能修改了应用页面。请关闭当前站点的自动翻译，然后重新加载。
            </p>
            <Button className="mt-5" onClick={this.handleReload}>
              重新加载页面
            </Button>
          </section>
        </main>
      );
    }

    return this.props.children;
  }
}
