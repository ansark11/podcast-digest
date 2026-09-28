import { getCollection, type CollectionEntry } from 'astro:content';

export type Summary = CollectionEntry<'summaries'>;

/** Every summary, newest first. */
export async function getEpisodes(): Promise<Summary[]> {
  const all = await getCollection('summaries');
  return all.sort((a, b) => (b.data.published ?? '').localeCompare(a.data.published ?? ''));
}

/**
 * Lenny's titles look like "Headline | Guest, Role". Split them so the list
 * shows a clean headline with the guest underneath. Titles without " | "
 * are kept whole.
 */
export function splitTitle(title: string): { headline: string; guest: string | null } {
  const i = title.lastIndexOf(' | ');
  if (i === -1) return { headline: title, guest: null };
  return { headline: title.slice(0, i).trim(), guest: title.slice(i + 3).trim() };
}

/** Short guest name for compact spots: "Adam Ward, Head of Talent" -> "Adam Ward". */
export function guestName(guest: string | null): string | null {
  if (!guest) return null;
  return guest.split(/[,(]/)[0].trim();
}

export function initials(text: string): string {
  const words = text.replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? '') + (words[1]?.[0] ?? '')).toUpperCase() || '•';
}

// Pastel tile colours for episodes without artwork: [tile, circle, text].
const TILES: [string, string, string][] = [
  ['#F8D5C6', '#EF9F84', '#7B2F18'],
  ['#D9DBFB', '#9EA2F0', '#2E2F86'],
  ['#F3E3C2', '#E0B866', '#6B4A0E'],
  ['#CDEBDD', '#7FC4A4', '#1C5A3F'],
  ['#E7D7F4', '#B790DA', '#4F2A72'],
  ['#FBD9E0', '#EE95A8', '#7A2338'],
];

export function tileColors(key: string): [string, string, string] {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TILES[h % TILES.length];
}

export function formatDate(iso: string | null, withYear = false): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}), timeZone: 'UTC',
  });
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (!seconds) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h} hr ${m} min` : `${m} min`;
}

/** Reading time at ~230 words per minute. */
export function readMinutes(s: Summary): number {
  const { overview, takeaways, quotes } = s.data.summary;
  const text = [overview, ...takeaways.map((t) => `${t.heading ?? ''} ${t.detail}`), ...quotes.map((q) => q.text)].join(' ');
  return Math.max(1, Math.round(text.split(/\s+/).length / 230));
}

/** Everything search should match on, lower-cased. */
export function searchText(s: Summary): string {
  const d = s.data;
  return [
    d.episode_title, d.show, d.summary.overview,
    ...d.summary.takeaways.map((t) => `${t.heading ?? ''} ${t.detail}`),
    ...d.summary.quotes.map((q) => `${q.text} ${q.speaker ?? ''}`),
    ...d.summary.resources.map((r) => r.name),
  ].join(' ').toLowerCase();
}

export function listenUrl(s: Summary): string | null {
  return s.data.episode_url ?? s.data.audio_url ?? null;
}
