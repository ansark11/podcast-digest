// Reading state kept in this browser only (no account, no sync between
// devices): which summaries you've opened, how far you've read, and saves.
const KEY = 'digest:v1';

export interface ReadingState {
  opened: Record<string, number>;   // id -> last opened (ms)
  progress: Record<string, number>; // id -> 0..1, furthest point reached
  saved: string[];                  // ids, most recent first
}

const empty = (): ReadingState => ({ opened: {}, progress: {}, saved: [] });

export function load(): ReadingState {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? { ...empty(), ...JSON.parse(raw) } : empty();
  } catch {
    return empty(); // private window, blocked storage: the app still works, it just forgets
  }
}

function save(state: ReadingState) {
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* ignore */ }
}

export function markOpened(id: string) {
  const s = load();
  s.opened[id] = Date.now();
  save(s);
}

export function setProgress(id: string, value: number) {
  const s = load();
  const v = Math.max(0, Math.min(1, value));
  if (v > (s.progress[id] ?? 0)) { s.progress[id] = v; save(s); }
}

export function toggleSaved(id: string): boolean {
  const s = load();
  const on = !s.saved.includes(id);
  s.saved = on ? [id, ...s.saved] : s.saved.filter((x) => x !== id);
  save(s);
  return on;
}

/** Started but not finished. */
export const IN_PROGRESS = (p: number | undefined) => p !== undefined && p > 0.03 && p < 0.95;
