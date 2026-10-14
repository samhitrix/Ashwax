export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** Returns a new tree with `value` set at `path`, leaving everything else untouched. */
export function setAtPath(
  obj: JsonValue,
  path: (string | number)[],
  value: JsonValue
): JsonValue {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  if (typeof head === 'number') {
    const arr: JsonValue[] = Array.isArray(obj) ? [...obj] : [];
    arr[head] = setAtPath(arr[head] ?? null, rest, value);
    return arr;
  }
  const source = obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
  const record: { [key: string]: JsonValue } = { ...source };
  record[head] = setAtPath(record[head] ?? null, rest, value);
  return record;
}

/** `_options` self-doc blocks in this repo's config files always sit at file root. */
export function findOptionsBlock(root: JsonValue): JsonValue | null {
  if (root && typeof root === 'object' && !Array.isArray(root) && '_options' in root) {
    return (root as { [key: string]: JsonValue })['_options'];
  }
  return null;
}

export interface OptionsLookupEntry {
  values: string[];
  descriptions?: Record<string, string>;
}

/**
 * Builds a dotted-path -> allowed-values lookup from a file's `_options` block.
 * Only structured entries become dropdowns: a real JSON array of strings, or an
 * object mapping value -> description string. A plain string entry (documentation
 * prose, e.g. universe.json's `_options.cache_universe`) is intentionally skipped —
 * there is no safe way to extract a value list from free text.
 */
export function buildOptionsLookup(
  optionsBlock: JsonValue | null
): Record<string, OptionsLookupEntry> {
  const lookup: Record<string, OptionsLookupEntry> = {};
  if (!optionsBlock || typeof optionsBlock !== 'object' || Array.isArray(optionsBlock)) {
    return lookup;
  }
  for (const [key, val] of Object.entries(optionsBlock)) {
    if (key.startsWith('_')) continue;
    if (Array.isArray(val) && val.every((v) => typeof v === 'string')) {
      lookup[key] = { values: val as string[] };
    } else if (val && typeof val === 'object' && !Array.isArray(val)) {
      const stringEntries = Object.entries(val as { [key: string]: JsonValue }).filter(
        (pair): pair is [string, string] => typeof pair[1] === 'string'
      );
      if (stringEntries.length > 0) {
        lookup[key] = {
          values: stringEntries.map(([v]) => v),
          descriptions: Object.fromEntries(stringEntries),
        };
      }
    }
  }
  return lookup;
}

/**
 * Builds a dotted-path -> prose lookup from a file's `_options` block, capturing
 * exactly the string-valued entries that `buildOptionsLookup` skips (free-text
 * documentation like global.json's `_options["system.master_switch"]`). Used to
 * surface helper text next to boolean toggles, which have no dropdown to attach
 * a description to.
 */
export function buildDescriptionsLookup(optionsBlock: JsonValue | null): Record<string, string> {
  const lookup: Record<string, string> = {};
  if (!optionsBlock || typeof optionsBlock !== 'object' || Array.isArray(optionsBlock)) {
    return lookup;
  }
  for (const [key, val] of Object.entries(optionsBlock)) {
    if (key.startsWith('_')) continue;
    if (typeof val === 'string') lookup[key] = val;
  }
  return lookup;
}

export function humanizeKey(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/** A same-shape "blank" clone of `value` — used to seed a new row when a user
 * adds an item to an array of objects, so the new item has the same fields as
 * its siblings instead of an empty `{}`. */
export function blankLike(value: JsonValue): JsonValue {
  if (typeof value === 'string') return '';
  if (typeof value === 'number') return 0;
  if (typeof value === 'boolean') return false;
  if (value === null) return null;
  if (Array.isArray(value)) return [];
  const result: { [key: string]: JsonValue } = {};
  for (const [k, v] of Object.entries(value)) result[k] = blankLike(v);
  return result;
}
