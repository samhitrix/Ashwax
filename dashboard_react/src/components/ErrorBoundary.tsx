import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props { children: ReactNode }
interface State { error: Error | null }

// Without this, one tab that throws while rendering unmounts the whole app and leaves a
// blank page. The boundary is keyed by the active tab in App.tsx, so picking another tab
// in the sidebar recovers without a reload.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ashwax] tab crashed:', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="m-6 rounded-xl border border-red-500/40 bg-red-500/10 p-6">
        <h2 className="text-lg font-semibold text-red-400">This tab hit an error</h2>
        <p className="mt-1 text-sm text-gray-400">
          The rest of the dashboard is still running. Pick another tab, or reload this page.
        </p>
        <pre className="mt-4 max-h-64 overflow-auto rounded bg-black/40 p-3 text-xs text-red-300">
          {error.message}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="mt-4 rounded-lg bg-gray-700 px-4 py-2 text-sm text-white hover:bg-gray-600"
        >
          Reload
        </button>
      </div>
    );
  }
}
