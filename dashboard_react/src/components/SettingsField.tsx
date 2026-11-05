import { useEffect, useState } from 'react';
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Bot,
  Building2,
  ChevronDown,
  ChevronRight,
  Clock,
  DollarSign,
  FlaskConical,
  Folder,
  ListOrdered,
  Lock,
  RefreshCw,
  ScaleIcon,
  Search,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { blankLike, humanizeKey, type JsonValue, type OptionsLookupEntry } from './settingsUtils';

interface SettingsFieldProps {
  fieldKey: string;
  value: JsonValue;
  path: (string | number)[];
  dottedPath: string;
  optionsLookup: Record<string, OptionsLookupEntry>;
  descriptionsLookup: Record<string, string>;
  onChange: (path: (string | number)[], value: JsonValue) => void;
}

const inputCls =
  'w-full bg-[#0d1117] border border-gray-700 rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500';

// Auto-derived identity fields inside array-of-object rows (e.g. each entry in
// strategies_registry.json's `strategies` array) — never something a user picks,
// so they're hidden from the editor rather than shown as a text input to edit.
const IDENTITY_FIELDS = new Set(['source', 'spec_path']);

// Best-effort icon for a nested section header, matched by keyword against the
// field's own key — there's no way to know a precise icon per arbitrary config
// key, so this degrades gracefully to a generic folder icon when nothing matches.
const SECTION_ICON_RULES: [RegExp, LucideIcon][] = [
  [/circuit|breaker/, ShieldAlert],
  [/risk/, AlertTriangle],
  [/limit|lock/, Lock],
  [/window|time|timeout|schedule/, Clock],
  [/cost|fee|price|rupee|premium/, DollarSign],
  [/order/, ListOrdered],
  [/volatility|vix/, Activity],
  [/drawdown/, TrendingDown],
  [/position|sizing|scale/, ScaleIcon],
  [/execution|rejection|retry/, RefreshCw],
  [/backtest/, FlaskConical],
  [/broker/, Building2],
  [/llm|ai|model|gemini|groq|ollama/, Bot],
  [/screener|scan/, Search],
  [/gap/, AlertCircle],
  [/universe|equity|stock|strategies|strategy/, TrendingUp],
  [/system|agent/, Zap],
];

function sectionIcon(fieldKey: string): LucideIcon {
  const key = fieldKey.toLowerCase();
  for (const [pattern, icon] of SECTION_ICON_RULES) {
    if (pattern.test(key)) return icon;
  }
  return Folder;
}

// Danger toggles — real-money kill switches that must look distinct from an
// ordinary toggle (e.g. dashboard.json display flags) and require confirmation.
const DANGER_FIELDS = new Set([
  'system.master_switch',
  'system.paper_mode',
  'system.segments.intraday.master_switch',
  'system.segments.swing.master_switch',
  'system.segments.options.master_switch',
  'system.segments.intraday.paper_mode',
  'system.segments.swing.paper_mode',
  'system.segments.options.paper_mode',
]);

/**
 * Controlled numeric input that tracks its own raw string while the user is
 * mid-edit, instead of round-tripping every keystroke through Number(). A
 * plain `value={numberValue}` input rewrites the DOM to the parsed-then-
 * restringified value on every keystroke — typing "0.25" character by
 * character hits an intermediate state where the browser's `.value` for the
 * incomplete float momentarily isn't a clean number, `Number(...)` collapses
 * it, and the decimal point gets clobbered (user ends up with "025" or "5").
 * Keeping raw text as the source of truth for display, and only pushing a
 * parsed number upstream when it's actually finite, avoids that.
 */
function NumberInput({
  value,
  resetKey,
  onChange,
  className,
}: {
  value: number;
  resetKey: string;
  onChange: (n: number) => void;
  className: string;
}) {
  const [raw, setRaw] = useState(String(value));

  useEffect(() => {
    setRaw(String(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, resetKey]);

  return (
    <input
      type="number"
      value={raw}
      onChange={(e) => {
        const next = e.target.value;
        setRaw(next);
        const parsed = Number(next);
        if (next.trim() !== '' && Number.isFinite(parsed)) {
          onChange(parsed);
        }
      }}
      className={className}
    />
  );
}

export function SettingsField({
  fieldKey,
  value,
  path,
  dottedPath,
  optionsLookup,
  descriptionsLookup,
  onChange,
}: SettingsFieldProps) {
  // Sections start minimised — clicking a sidebar tab should reveal tiles to
  // drill into, not dump every nested field open at once.
  const [collapsed, setCollapsed] = useState(true);

  // 1. Comment/note/self-doc keys — read-only, always visible, never editable.
  if (fieldKey.startsWith('_')) {
    if (typeof value !== 'string') return null;
    return (
      <p className="text-xs text-gray-500 italic border-l-2 border-gray-700 pl-2 mb-2">
        {value}
      </p>
    );
  }

  // 2. Boolean — toggle switch.
  if (typeof value === 'boolean') {
    const description = descriptionsLookup[dottedPath];
    const isDanger = DANGER_FIELDS.has(dottedPath);
    return (
      <div className="flex items-center justify-between py-2 gap-4">
        <div className="min-w-0">
          <span className="text-sm text-gray-300">{humanizeKey(fieldKey)}</span>
          {description && <p className="text-xs text-gray-500 mt-0.5">{description}</p>}
        </div>
        <button
          type="button"
          onClick={() => {
            if (isDanger && !window.confirm(`Change ${dottedPath} to ${!value}? This affects live trading.`)) {
              return;
            }
            onChange(path, !value);
          }}
          className={`w-12 h-7 rounded-full p-1 shrink-0 transition-colors ${
            isDanger
              ? `ring-2 ring-offset-1 ring-offset-[#161b22] ring-red-500 ${value ? 'bg-green-500' : 'bg-red-600'}`
              : value
                ? 'bg-green-500'
                : 'bg-gray-600'
          }`}
        >
          <div
            className={`w-5 h-5 rounded-full bg-white transition-transform ${
              value ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
      </div>
    );
  }

  // 3. String or number — dropdown if a documented option set matches, else input.
  if (typeof value === 'string' || typeof value === 'number') {
    const opts = optionsLookup[dottedPath];
    if (opts) {
      return (
        <div className="py-2">
          <label className="block text-sm text-gray-300 mb-1">{humanizeKey(fieldKey)}</label>
          <select
            value={String(value)}
            onChange={(e) =>
              onChange(path, typeof value === 'number' ? Number(e.target.value) : e.target.value)
            }
            className={inputCls}
          >
            {opts.values.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
          {opts.descriptions?.[String(value)] && (
            <p className="text-xs text-gray-500 mt-1">{opts.descriptions[String(value)]}</p>
          )}
        </div>
      );
    }
    return (
      <div className="py-2">
        <label className="block text-sm text-gray-300 mb-1">{humanizeKey(fieldKey)}</label>
        {typeof value === 'number' ? (
          <NumberInput
            value={value}
            resetKey={dottedPath}
            onChange={(n) => onChange(path, n)}
            className={inputCls}
          />
        ) : (
          <input
            type="text"
            value={value}
            onChange={(e) => onChange(path, e.target.value)}
            className={inputCls}
          />
        )}
      </div>
    );
  }

  // 4. null — rare (e.g. index_filter: null); editable as optional text.
  if (value === null) {
    return (
      <div className="py-2">
        <label className="block text-sm text-gray-300 mb-1">{humanizeKey(fieldKey)}</label>
        <input
          type="text"
          value=""
          placeholder="null"
          onChange={(e) => onChange(path, e.target.value === '' ? null : e.target.value)}
          className={inputCls}
        />
      </div>
    );
  }

  // 5. Array of strings — multi-select if documented, else add/remove text rows.
  if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
    const items = value as string[];
    const opts = optionsLookup[dottedPath];
    if (opts) {
      const selected = new Set(items);
      return (
        <div className="py-2">
          <label className="block text-sm text-gray-300 mb-1">{humanizeKey(fieldKey)}</label>
          <div className="flex flex-wrap gap-2">
            {opts.values.map((v) => (
              <label
                key={v}
                className="flex items-center gap-1.5 text-xs bg-[#0d1117] border border-gray-700 rounded-lg px-2 py-1 text-gray-300"
              >
                <input
                  type="checkbox"
                  checked={selected.has(v)}
                  onChange={(e) => {
                    const next = new Set(selected);
                    if (e.target.checked) next.add(v);
                    else next.delete(v);
                    onChange(path, Array.from(next));
                  }}
                />
                {v}
              </label>
            ))}
          </div>
        </div>
      );
    }
    return (
      <div className="py-2">
        <label className="block text-sm text-gray-300 mb-1">{humanizeKey(fieldKey)}</label>
        <div className="space-y-1.5">
          {items.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                value={item}
                onChange={(e) => {
                  const next = [...items];
                  next[i] = e.target.value;
                  onChange(path, next);
                }}
                className={inputCls}
              />
              <button
                type="button"
                onClick={() => onChange(path, items.filter((_, idx) => idx !== i))}
                className="text-red-400 hover:text-red-300 text-xs px-2 shrink-0"
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() => onChange(path, [...items, ''])}
            className="text-blue-400 hover:text-blue-300 text-xs"
          >
            + Add
          </button>
        </div>
      </div>
    );
  }

  // 6. Array of objects — repeating cards, each field recursing through the same rules.
  if (Array.isArray(value)) {
    // Rows shaped like strategies_registry.json's `strategies` entries carry
    // IDENTITY_FIELDS (source/spec_path) that this generic UI hides — a
    // hand-added row could never fill them in and risks a broken registry
    // entry. New strategies get registered via PUT /api/strategies/{name}/enabled
    // instead, so skip offering "+ Add" for this shape.
    const templateRow = value.length > 0 ? value[value.length - 1] : null;
    const isIdentityShaped =
      templateRow &&
      typeof templateRow === 'object' &&
      !Array.isArray(templateRow) &&
      Object.keys(templateRow as { [key: string]: JsonValue }).some((k) => IDENTITY_FIELDS.has(k));
    return (
      <div className="py-2">
        <label className="block text-sm text-gray-300 mb-2">{humanizeKey(fieldKey)}</label>
        <div className="space-y-3">
          {value.map((item, i) => (
            <div
              key={i}
              className="bg-[#0d1117] border border-gray-800 rounded-lg p-3 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                {item && typeof item === 'object' && !Array.isArray(item)
                  ? Object.entries(item as { [key: string]: JsonValue })
                      .filter(([k]) => !IDENTITY_FIELDS.has(k))
                      .map(([k, v]) => (
                        <SettingsField
                          key={k}
                          fieldKey={k}
                          value={v}
                          path={[...path, i, k]}
                          dottedPath={`${dottedPath}.${k}`}
                          optionsLookup={optionsLookup}
                          descriptionsLookup={descriptionsLookup}
                          onChange={onChange}
                        />
                      ))
                  : null}
              </div>
              <button
                type="button"
                onClick={() => onChange(path, value.filter((_, idx) => idx !== i))}
                className="text-red-400 hover:text-red-300 text-xs px-2 shrink-0"
              >
                Remove
              </button>
            </div>
          ))}
        </div>
        {!isIdentityShaped && (
          <button
            type="button"
            onClick={() =>
              onChange(path, [...value, value.length > 0 ? blankLike(value[value.length - 1]) : {}])
            }
            className="text-blue-400 hover:text-blue-300 text-xs mt-2"
          >
            + Add
          </button>
        )}
      </div>
    );
  }

  // 7. Plain object — collapsible section, recursing into its own entries. The
  // whole header bar is the click target (not just the chevron/label), and it's
  // styled as a filled bar so nested sections read clearly at a glance.
  const entries = Object.entries(value);
  const SectionIcon = sectionIcon(fieldKey);
  return (
    <div className="py-1.5">
      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-[#0d1117] hover:bg-[#0d1117]/70 border border-gray-800 hover:border-gray-700 text-[13px] font-semibold text-gray-100 tracking-wide transition-colors"
      >
        {collapsed ? (
          <ChevronRight className="w-4 h-4 text-gray-500 shrink-0" />
        ) : (
          <ChevronDown className="w-4 h-4 text-gray-500 shrink-0" />
        )}
        <SectionIcon className="w-4 h-4 text-blue-400/80 shrink-0" />
        {humanizeKey(fieldKey)}
      </button>
      {!collapsed && (
        <div className="pl-4 pr-1 pt-2 pb-1 ml-2 border-l-2 border-gray-800">
          {entries.map(([k, v]) => (
            <SettingsField
              key={k}
              fieldKey={k}
              value={v}
              path={[...path, k]}
              dottedPath={dottedPath ? `${dottedPath}.${k}` : k}
              optionsLookup={optionsLookup}
              descriptionsLookup={descriptionsLookup}
              onChange={onChange}
            />
          ))}
        </div>
      )}
    </div>
  );
}
