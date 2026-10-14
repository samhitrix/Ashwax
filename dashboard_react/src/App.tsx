import { useEffect, lazy, Suspense } from 'react';
import { Layout } from './components/Layout';

// Overview is the default tab, so it stays in the main bundle — lazy-loading the
// landing view would only add a spinner to the very first paint.
import { Overview } from './components/Overview';
import { useTradingStore } from './store/tradingStore';
import { useAdvancedStore } from './store/advancedStore';

// Every other tab is code-split. All 22 were previously imported eagerly, so the
// browser parsed the entire dashboard (870kB of JS) before it could render the one
// tab actually being viewed. `lazy` needs a default export; these are named, hence
// the .then() unwrap.
const OrderMonitor = lazy(() => import('./components/OrderMonitor').then((m) => ({ default: m.OrderMonitor })));
const Alerts = lazy(() => import('./components/Alerts').then((m) => ({ default: m.Alerts })));
const NewsSentiment = lazy(() => import('./components/NewsSentiment').then((m) => ({ default: m.NewsSentiment })));
const SelfHealing = lazy(() => import('./components/SelfHealing').then((m) => ({ default: m.SelfHealing })));
const AIInsights = lazy(() => import('./components/AIInsights').then((m) => ({ default: m.AIInsights })));
const AgentStatus = lazy(() => import('./components/AgentStatus').then((m) => ({ default: m.AgentStatus })));
const Configuration = lazy(() => import('./components/Configuration').then((m) => ({ default: m.Configuration })));
const AdvancedOrderMonitor = lazy(() => import('./components/AdvancedOrderMonitor').then((m) => ({ default: m.AdvancedOrderMonitor })));
const RealTimeNews = lazy(() => import('./components/RealTimeNews').then((m) => ({ default: m.RealTimeNews })));
const AdvancedSelfHealing = lazy(() => import('./components/AdvancedSelfHealing').then((m) => ({ default: m.AdvancedSelfHealing })));
const Intelligence = lazy(() => import('./components/Intelligence').then((m) => ({ default: m.Intelligence })));
const Learning = lazy(() => import('./components/Learning'));
const StrategyStudio = lazy(() => import('./components/StrategyStudio').then((m) => ({ default: m.StrategyStudio })));
const SystemHealth = lazy(() => import('./components/SystemHealth').then((m) => ({ default: m.SystemHealth })));
const Trades = lazy(() => import('./components/Trades').then((m) => ({ default: m.Trades })));
const PaperTrades = lazy(() => import('./components/PaperTrades').then((m) => ({ default: m.PaperTrades })));
const OptionsTrading = lazy(() => import('./components/OptionsTrading').then((m) => ({ default: m.OptionsTrading })));
const Reports = lazy(() => import('./components/Reports').then((m) => ({ default: m.Reports })));
const SignalScanner = lazy(() => import('./components/SignalScanner').then((m) => ({ default: m.SignalScanner })));
const Logs = lazy(() => import('./components/Logs').then((m) => ({ default: m.Logs })));

// How often the two global store refreshes are kicked off. Was 2000ms, which was
// shorter than a cycle ever took to complete: a full cycle is 27 HTTP requests and
// measured 6.6-16.7s wall under real concurrency, so every tick landed on a cycle
// still in flight and the backlog never drained. Endpoints that answer in 0.21s on
// an idle server were taking 8-9s under that self-inflicted load — past the 8s
// client timeout, which is what flipped the agent badge to "Stopped".
const REFRESH_INTERVAL_MS = 5000;

function AppContent() {
  // Selector subscriptions, not `useTradingStore()`. A bare store call re-renders
  // this component — and therefore the whole active tab tree below it — on EVERY
  // store write, and one refresh cycle performs ~19 separate writes.
  const activeTab = useTradingStore((s) => s.activeTab);
  const refreshData = useTradingStore((s) => s.refreshData);
  const refreshAdvancedData = useAdvancedStore((s) => s.refreshAdvancedData);

  useEffect(() => {
    // Warm BOTH stores globally so switching tabs shows populated data immediately
    // instead of a blank-then-load flash (each tab no longer waits for its own mount
    // to start fetching).
    refreshData();
    refreshAdvancedData();
    const interval = setInterval(() => {
      refreshData();
      refreshAdvancedData();
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [refreshData, refreshAdvancedData]);

  const renderContent = () => {
    switch (activeTab) {
      case 'overview':
        return <Overview />;
      case 'positions':
        return <OrderMonitor />;
      case 'alerts':
        return <Alerts />;
      case 'news':
        return <NewsSentiment />;
      case 'self-healing':
        return <SelfHealing />;
      case 'insights':
        return <AIInsights />;
      case 'agent':
        return <AgentStatus />;
      case 'config':
        return <Configuration />;
      // Advanced Features (Roadmap Implementations)
      case 'advanced-orders':
        return <AdvancedOrderMonitor />;
      case 'realtime-news':
        return <RealTimeNews />;
      case 'advanced-healing':
        return <AdvancedSelfHealing />;
      case 'intelligence':
        return <Intelligence />;
      case 'learning':
        return <Learning />;
      case 'strategy-studio':
        return <StrategyStudio />;
      case 'trades':
        return <Trades />;
      case 'paper-trades':
        return <PaperTrades />;
      case 'option-chain':
        return <OptionsTrading />;
      case 'signals':
        return <SignalScanner />;
      case 'system-health':
        return <SystemHealth />;
      case 'reports':
        return <Reports />;
      case 'logs':
        return <Logs />;
      default:
        return <Overview />;
    }
  };

  return (
    <Layout>
      <Suspense fallback={<div className="p-6 text-sm opacity-60">Loading…</div>}>
        {renderContent()}
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return <AppContent />;
}
