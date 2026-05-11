// Thin wrappers over chrome.storage.local with sensible defaults.
// Falls back to localStorage when running outside an extension context
// (so unit tests don't have to mock chrome.*).

export interface UserSettings {
  audioLatencyOffsetMs: number;   // calibration result, additive offset
  noteSpeed: number;              // pixels per second, scale factor
  laneWidthPx: number;
  opacity: number;                // 0-1
  bindings: [string, string, string, string]; // lane 0..3 -> key code
  sfxEnabled: boolean;
}

export const DEFAULT_SETTINGS: UserSettings = {
  audioLatencyOffsetMs: 0,
  noteSpeed: 1.0,
  laneWidthPx: 96,
  opacity: 0.92,
  bindings: ["KeyD", "KeyF", "KeyJ", "KeyK"],
  sfxEnabled: false,
};

const SETTINGS_KEY = "beatbridge.settings";
const SCORES_KEY = "beatbridge.scores";

type ScoreEntry = {
  videoId: string;
  difficulty: string;
  bestScore: number;
  bestAccuracy: number;
  updatedAt: string;
};

function hasChromeStorage(): boolean {
  return typeof chrome !== "undefined" && !!chrome?.storage?.local;
}

async function readKey<T>(key: string, fallback: T): Promise<T> {
  if (hasChromeStorage()) {
    return new Promise((resolve) => {
      chrome.storage.local.get(key, (items) => {
        const v = items[key];
        resolve(v === undefined ? fallback : (v as T));
      });
    });
  }
  try {
    const raw = globalThis.localStorage?.getItem(key);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

async function writeKey<T>(key: string, value: T): Promise<void> {
  if (hasChromeStorage()) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [key]: value }, () => resolve());
    });
  }
  try {
    globalThis.localStorage?.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

export async function loadSettings(): Promise<UserSettings> {
  const partial = await readKey<Partial<UserSettings>>(SETTINGS_KEY, {});
  return { ...DEFAULT_SETTINGS, ...partial };
}

export async function saveSettings(s: UserSettings): Promise<void> {
  await writeKey(SETTINGS_KEY, s);
}

export async function loadBestScore(
  videoId: string,
  difficulty: string,
): Promise<ScoreEntry | null> {
  const all = await readKey<Record<string, ScoreEntry>>(SCORES_KEY, {});
  return all[`${videoId}::${difficulty}`] ?? null;
}

export async function saveBestScore(entry: ScoreEntry): Promise<void> {
  const all = await readKey<Record<string, ScoreEntry>>(SCORES_KEY, {});
  const key = `${entry.videoId}::${entry.difficulty}`;
  const existing = all[key];
  if (!existing || entry.bestScore > existing.bestScore) {
    all[key] = entry;
    await writeKey(SCORES_KEY, all);
  }
}
