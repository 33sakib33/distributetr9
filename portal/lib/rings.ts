// Update rings: a PC updates when its ring is at or below the rollout ring.
// The API accepts 0 to 9; the lab uses the first three.
export const RING_NAMES: Record<number, string> = { 0: "canary", 1: "early", 2: "everyone" };
export const RINGS = Array.from({ length: 10 }, (_, i) => i);

export function ringLabel(ring: number) {
  const name = RING_NAMES[ring];
  return name ? `${ring} ${name}` : String(ring);
}
