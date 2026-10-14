import { type ReactNode } from 'react';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  FileText,
  Newspaper,
  Search,
  Settings,
  Shield,
  TrendingUp,
  Zap,
  Eye,
  Brain,
  FlaskConical,
  BookOpen,
  Sun,
  Moon,
  ScrollText,
  Link2,
} from 'lucide-react';
import { useTradingStore } from '../store/tradingStore';
import { useThemeStore } from '../store/themeStore';
import { cn } from '../utils/cn';
import { AlertToast } from './AlertToast';

interface NavItem {
  id: string;
  label: string;
  icon: ReactNode;
  badge?: number;
}

const navItems: NavItem[] = [
  { id: 'overview', label: 'Overview', icon: <BarChart3 className="w-5 h-5" /> },
  { id: 'advanced-orders', label: '⚡ Order Monitor+', icon: <Eye className="w-5 h-5" /> },
  { id: 'option-chain', label: '📊 Options Trading', icon: <Link2 className="w-5 h-5" /> },
  { id: 'paper-trades', label: '📝 Paper Trades', icon: <FileText className="w-5 h-5" /> },
  { id: 'realtime-news', label: '📰 News Intel', icon: <Newspaper className="w-5 h-5" /> },
  { id: 'signals', label: '🔍 Signals/Scanner', icon: <Search className="w-5 h-5" /> },
  { id: 'strategy-studio', label: '🧪 Strategy Studio', icon: <FlaskConical className="w-5 h-5" /> },
  { id: 'intelligence', label: '🧠 Intelligence', icon: <Brain className="w-5 h-5" /> },
  { id: 'learning', label: '🎓 Learning', icon: <BookOpen className="w-5 h-5" /> },
  { id: 'agent', label: 'Agent Status', icon: <Bot className="w-5 h-5" /> },
  { id: 'system-health', label: '🛡️ System Health', icon: <Shield className="w-5 h-5" /> },
  { id: 'advanced-healing', label: '🔧 Self-Healing+', icon: <Zap className="w-5 h-5" /> },
  { id: 'config', label: 'Configuration', icon: <Settings className="w-5 h-5" /> },
  { id: 'alerts', label: 'Alerts', icon: <AlertTriangle className="w-5 h-5" /> },
  { id: 'trades', label: '💰 Trades & P&L', icon: <TrendingUp className="w-5 h-5" /> },
  { id: 'reports', label: '📋 Reports', icon: <FileText className="w-5 h-5" /> },
  { id: 'logs', label: 'Logs', icon: <ScrollText className="w-5 h-5" /> },
];

interface LayoutProps {
  children: ReactNode;
}

export function Layout({ children }: LayoutProps) {
  // One selector per field rather than a bare useTradingStore(): Layout renders the
  // entire left nav and header, and a whole-store subscription re-rendered all of it
  // on every one of the ~19 writes each refresh cycle performs — including writes to
  // slices (orders, gttOrders, breaches…) this component never reads.
  const activeTab = useTradingStore((s) => s.activeTab);
  const setActiveTab = useTradingStore((s) => s.setActiveTab);
  const systemAlerts = useTradingStore((s) => s.systemAlerts);
  const hasUnseenCriticalAlert = useTradingStore((s) => s.hasUnseenCriticalAlert);
  const agentState = useTradingStore((s) => s.agentState);
  const insights = useTradingStore((s) => s.insights);
  const positions = useTradingStore((s) => s.positions);
  const marketContext = useTradingStore((s) => s.marketContext);
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);

  const unresolvedAlerts = systemAlerts.filter((a) => a.tier === 'CRITICAL' || a.tier === 'EMERGENCY').length;
  const activeInsights = insights.filter((i) => i.actionable).length;

  // Add badges dynamically
  const itemsWithBadges = navItems.map((item) => {
    if (item.id === 'alerts' && unresolvedAlerts > 0) {
      return { ...item, badge: unresolvedAlerts };
    }
    if (item.id === 'insights' && activeInsights > 0) {
      return { ...item, badge: activeInsights };
    }
    if (item.id === 'positions') {
      return { ...item, badge: positions.length };
    }
    return item;
  });

  return (
    <div className="min-h-screen bg-[#0d1117] text-gray-100">
      {/* Header */}
      <header className="fixed top-0 left-0 right-0 z-50 bg-[#161b22] border-b border-gray-800">
        <div className="flex items-center justify-between px-6 py-3">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500 to-cyan-500 flex items-center justify-center">
                <Activity className="w-6 h-6 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-bold text-white">AshwaX</h1>
                <p className="text-xs text-gray-400">Autonomous Trading System</p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-6">
            {/* Market Status */}
            <div className="flex items-center gap-4 px-4 py-2 bg-[#0d1117] rounded-lg">
              <div className="text-right">
                <div className="text-xs text-gray-400">NIFTY 50</div>
                <div className="font-mono font-semibold">
                  {marketContext.niftyLevel > 0 ? marketContext.niftyLevel.toLocaleString() : '—'}
                </div>
              </div>
              {marketContext.niftyLevel > 0 && (
                <div className={cn(
                  'px-2 py-1 rounded text-xs font-medium',
                  marketContext.niftyChangePercent >= 0 ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
                )}>
                  {marketContext.niftyChangePercent >= 0 ? '+' : ''}{marketContext.niftyChangePercent.toFixed(2)}%
                </div>
              )}
              <div className="border-l border-gray-700 pl-4">
                <div className="text-xs text-gray-400">VIX</div>
                <div className="font-mono text-sm">
                  {marketContext.vixLevel > 0 ? marketContext.vixLevel.toFixed(2) : '—'}
                </div>
              </div>
            </div>

            {/* Agent Status Pill */}
            <div className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium',
              agentState.isActive
                ? 'bg-green-500/20 text-green-400 border border-green-500/30'
                : 'bg-red-500/20 text-red-400 border border-red-500/30'
            )}>
              <div className={cn(
                'w-2 h-2 rounded-full',
                agentState.isActive ? 'bg-green-400 animate-pulse' : 'bg-red-400'
              )} />
              {agentState.isActive ? 'Agent Active' : 'Agent Stopped'}
              <span className="text-xs opacity-70">({agentState.mode})</span>
            </div>

            {/* Circuit Breaker Status */}
            <div className="flex items-center gap-2">
              <Shield className={cn(
                'w-5 h-5',
                Object.values(agentState.circuitBreakers ?? {}).some((cb) =>
                  'triggered' in cb ? cb.triggered : ('active' in cb && cb.active)
                )
                  ? 'text-red-400'
                  : 'text-green-400'
              )} />
            </div>

            {/* Theme toggle */}
            <button
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              className="p-2 rounded-lg text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
            >
              {theme === 'dark' ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
            </button>
          </div>
        </div>
      </header>

      {/* Sidebar */}
      <aside className="fixed left-0 top-16 bottom-0 w-64 bg-[#161b22] border-r border-gray-800 flex flex-col">
        <nav className="flex-1 min-h-0 overflow-y-auto p-4 space-y-1">
          {itemsWithBadges.map((item) => (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              className={cn(
                'w-full flex items-center justify-between px-4 py-3 rounded-lg text-sm font-medium transition-all',
                activeTab === item.id
                  ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
                  : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200',
                // Points at where the toast's alert actually lives until the user
                // opens this tab (cleared in setActiveTab).
                item.id === 'alerts' && hasUnseenCriticalAlert && activeTab !== 'alerts' && 'animate-pulse'
              )}
            >
              <div className="flex items-center gap-3">
                {/* Labels that start with an emoji already carry a colored icon —
                    only render the b&w lucide icon for the plain (ASCII) labels. */}
                {!/^[^\x00-\x7F]/.test(item.label) && item.icon}
                {item.label}
              </div>
              {item.badge !== undefined && item.badge > 0 && (
                <span className={cn(
                  'px-2 py-0.5 rounded-full text-xs font-semibold',
                  item.id === 'alerts'
                    ? 'bg-red-500/20 text-red-400'
                    : 'bg-blue-500/20 text-blue-400'
                )}>
                  {item.badge}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* P&L Summary */}
        <div className="shrink-0 p-4 bg-[#0d1117] border-t border-gray-800">
          <div className="space-y-3">
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-400">Today's P&L</span>
              <span className={cn(
                'font-mono font-semibold',
                agentState.dailyPnL >= 0 ? 'text-green-400' : 'text-red-400'
              )}>
                {agentState.dailyPnL >= 0 ? '+' : ''}₹{agentState.dailyPnL.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-400">Open Positions</span>
              <span className="font-mono text-sm">
                {agentState.openPositions}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-400">Exposure</span>
              <span className="font-mono text-sm">
                ₹{(agentState.totalExposure / 1000).toFixed(0)}K
              </span>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="ml-64 pt-16 min-h-screen">
        <div className="p-6">
          {children}
        </div>
      </main>

      <AlertToast />
    </div>
  );
}
