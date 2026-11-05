import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  BarChart2,
  Bell,
  BookOpen,
  Brain,
  Calendar,
  Clock,
  Eye,
  FlaskConical,
  Globe,
  LayoutDashboard,
  Newspaper,
  ReceiptIndianRupee,
  RefreshCw,
  Save,
  Settings,
  Shield,
  TrendingUp,
  UserCog,
  Wallet,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { SettingsField } from './SettingsField';
import {
  buildDescriptionsLookup,
  buildOptionsLookup,
  findOptionsBlock,
  setAtPath,
  type JsonValue,
} from './settingsUtils';

// Order files appear in the sidebar — mirrors config/ file list at design time.
const FILE_ORDER = [
  'global',
  'operator',
  'capital',
  'risk',
  'market_hours',
  'execution',
  'charges',
  'options_settings',
  'strategies_registry',
  'universe',
  'intelligence',
  'news',
  'supervisor',
  'dashboard',
  'nse_expiry_calendar',
  'nse_position_limits',
  'learning',
  'notifications',
];

interface FileMeta {
  label: string;
  icon: LucideIcon;
  color: string;
}

const FILE_META: Record<string, FileMeta> = {
  global: { label: 'Global', icon: Settings, color: 'text-blue-400' },
  operator: { label: 'Operator', icon: UserCog, color: 'text-amber-400' },
  capital: { label: 'Capital', icon: Wallet, color: 'text-emerald-400' },
  risk: { label: 'Risk', icon: Shield, color: 'text-red-400' },
  market_hours: { label: 'Market Hours', icon: Clock, color: 'text-teal-400' },
  execution: { label: 'Execution', icon: Zap, color: 'text-yellow-400' },
  charges: { label: 'Charges', icon: ReceiptIndianRupee, color: 'text-lime-400' },
  options_settings: { label: 'Options', icon: TrendingUp, color: 'text-purple-400' },
  strategies_registry: { label: 'Strategies', icon: FlaskConical, color: 'text-purple-400' },
  universe: { label: 'Universe', icon: Globe, color: 'text-cyan-400' },
  intelligence: { label: 'Intelligence', icon: Brain, color: 'text-pink-400' },
  news: { label: 'News', icon: Newspaper, color: 'text-blue-400' },
  supervisor: { label: 'Supervisor', icon: Eye, color: 'text-green-400' },
  dashboard: { label: 'Dashboard', icon: LayoutDashboard, color: 'text-gray-400' },
  nse_expiry_calendar: { label: 'Expiry Calendar', icon: Calendar, color: 'text-cyan-400' },
  nse_position_limits: { label: 'Position Limits', icon: BarChart2, color: 'text-orange-400' },
  learning: { label: 'Learning', icon: BookOpen, color: 'text-green-400' },
  notifications: { label: 'Notifications', icon: Bell, color: 'text-yellow-400' },
};

const DEFAULT_META: FileMeta = { label: 'Config', icon: Settings, color: 'text-gray-400' };

// Only strategies_registry.json is re-read at runtime (orchestrator._reload_loop,
// every ~300s). Every other config file is read ONCE at startup inside
// orchestrator._run() — edits to those need a launcher restart to take effect.
const HOT_RELOADED_FILES = new Set(['strategies_registry']);

interface FileState {
  data: JsonValue;
  dirty: boolean;
  saving: boolean;
  error: string | null;
}

export function Configuration() {
  const [files, setFiles] = useState<Record<string, FileState>>({});
  const [loaded, setLoaded] = useState(false);
  const [activeFile, setActiveFile] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/settings/files');
        const list: { name: string }[] = await res.json();
        const entries = await Promise.all(
          list.map(async ({ name }) => {
            const r = await fetch(`/api/settings/${name}`);
            const data: JsonValue = await r.json();
            return [name, { data, dirty: false, saving: false, error: null }] as const;
          })
        );
        if (!cancelled) {
          const loadedFiles = Object.fromEntries(entries);
          setFiles(loadedFiles);
          setLoaded(true);
          const firstOrdered = FILE_ORDER.find((n) => n in loadedFiles);
          setActiveFile(firstOrdered ?? Object.keys(loadedFiles)[0] ?? null);
        }
      } catch (e) {
        console.error('[ashwax] failed to load settings', e);
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Any config file not in the hardcoded FILE_ORDER list (e.g. one added later
  // to config/*.json) still shows up — just appended at the end.
  const orderedNames = [
    ...FILE_ORDER.filter((n) => n in files),
    ...Object.keys(files).filter((n) => !FILE_ORDER.includes(n)),
  ];

  const handleChange = (fileName: string, path: (string | number)[], value: JsonValue) => {
    setFiles((prev) => {
      const file = prev[fileName];
      if (!file) return prev;
      return {
        ...prev,
        [fileName]: { ...file, data: setAtPath(file.data, path, value), dirty: true, error: null },
      };
    });
  };

  const handleSave = async (fileName: string) => {
    setFiles((prev) => ({ ...prev, [fileName]: { ...prev[fileName], saving: true } }));
    try {
      const res = await fetch(`/api/settings/${fileName}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: files[fileName].data }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Re-fetch from disk rather than trusting the in-memory copy — something
      // else (e.g. src/supervisor/remediator.py autonomously rewriting
      // global.json) could have written the file in the meantime.
      const fresh = await fetch(`/api/settings/${fileName}`);
      const freshData: JsonValue = await fresh.json();
      setFiles((prev) => ({
        ...prev,
        [fileName]: { ...prev[fileName], data: freshData, dirty: false, saving: false, error: null },
      }));
    } catch (e) {
      setFiles((prev) => ({
        ...prev,
        [fileName]: {
          ...prev[fileName],
          saving: false,
          error: e instanceof Error ? e.message : 'Save failed',
        },
      }));
    }
  };

  const dirtyNames = orderedNames.filter((n) => files[n]?.dirty);
  const anyDirty = dirtyNames.length > 0;

  const handleSaveAll = () => {
    dirtyNames.forEach((name) => handleSave(name));
  };

  const activeFileState = activeFile ? files[activeFile] : null;
  const activeMeta = activeFile ? (FILE_META[activeFile] ?? DEFAULT_META) : DEFAULT_META;
  const ActiveIcon = activeMeta.icon;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Configuration</h2>
          <p className="text-gray-400">Live-edit every config/*.json file the trading engine reads</p>
        </div>
        <div className="flex items-center gap-4">
          {anyDirty && (
            <span className="text-sm text-yellow-400 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4" />
              Unsaved changes
            </span>
          )}
          <button
            onClick={handleSaveAll}
            disabled={!anyDirty}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              anyDirty ? 'bg-green-500 text-white hover:bg-green-600' : 'bg-gray-700 text-gray-400'
            }`}
          >
            <Save className="w-4 h-4" />
            Save Changes
          </button>
        </div>
      </div>

      {!loaded ? (
        <p className="text-gray-500 text-sm">Loading configuration…</p>
      ) : orderedNames.length === 0 ? (
        <p className="text-gray-500 text-sm">No config files found.</p>
      ) : (
        <div className="flex gap-6 items-start">
          {/* Sidebar — one entry per config file */}
          <div className="w-56 shrink-0 space-y-1">
            {orderedNames.map((name) => {
              const meta = FILE_META[name] ?? DEFAULT_META;
              const Icon = meta.icon;
              const file = files[name];
              const active = name === activeFile;
              return (
                <button
                  key={name}
                  onClick={() => setActiveFile(name)}
                  className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-left transition-colors border ${
                    active
                      ? 'bg-[#161b22] text-white border-gray-700'
                      : 'text-gray-400 border-transparent hover:bg-[#161b22]/60 hover:text-gray-200'
                  }`}
                >
                  <Icon className={`w-4 h-4 shrink-0 ${meta.color}`} />
                  <span className="flex-1 truncate">{meta.label}</span>
                  {file?.dirty && <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 shrink-0" />}
                </button>
              );
            })}
          </div>

          {/* Active file's fields */}
          <div className="flex-1 min-w-0">
            {activeFile && activeFileState && (() => {
              const file = activeFileState;
              if (!file.data || typeof file.data !== 'object' || Array.isArray(file.data)) return null;
              const root = file.data as { [key: string]: JsonValue };
              const optionsBlock = findOptionsBlock(root);
              const optionsLookup = buildOptionsLookup(optionsBlock);
              const descriptionsLookup = buildDescriptionsLookup(optionsBlock);
              return (
                <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-hidden">
                  <div className="p-4 border-b border-gray-800 flex items-center justify-between">
                    <h3 className="font-semibold text-white flex items-center gap-2">
                      <ActiveIcon className={`w-5 h-5 ${activeMeta.color}`} />
                      {activeMeta.label}
                      <span className="text-xs text-gray-500 font-normal">{activeFile}.json</span>
                      {!HOT_RELOADED_FILES.has(activeFile) && (
                        <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400">
                          restart required
                        </span>
                      )}
                    </h3>
                    <div className="flex items-center gap-3">
                      {file.error && <span className="text-xs text-red-400">{file.error}</span>}
                      {file.saving && <RefreshCw className="w-4 h-4 text-gray-400 animate-spin" />}
                      {file.dirty && !file.saving && (
                        <button
                          onClick={() => handleSave(activeFile)}
                          className="text-xs px-3 py-1 rounded bg-green-500/20 text-green-400 hover:bg-green-500/30"
                        >
                          Save {activeFile}.json
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="p-4">
                    {Object.entries(root).map(([k, v]) => (
                      <SettingsField
                        key={k}
                        fieldKey={k}
                        value={v}
                        path={[k]}
                        dottedPath={k}
                        optionsLookup={optionsLookup}
                        descriptionsLookup={descriptionsLookup}
                        onChange={(path, value) => handleChange(activeFile, path, value)}
                      />
                    ))}
                  </div>
                </div>
              );
            })()}
          </div>
        </div>
      )}
    </div>
  );
}
