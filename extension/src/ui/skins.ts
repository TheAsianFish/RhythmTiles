export const SKIN_IDS = ["midnight", "aurora", "arcade", "ember", "pro"] as const;

export type SkinId = (typeof SKIN_IDS)[number];

/** Human labels for selects. */
export const SKIN_LABELS: Record<SkinId, string> = {
  midnight: "Midnight",
  aurora: "Aurora",
  arcade: "Arcade",
  ember: "Ember",
  pro: "Pro",
};

export function coerceSkinId(v: unknown): SkinId {
  if (typeof v === "string" && (SKIN_IDS as readonly string[]).includes(v)) {
    return v as SkinId;
  }
  return "midnight";
}
