/// <reference types="vite/client" />
import type { Packet, Summary } from "./types";

// `vite build --mode static` bundles the committed packets and answers the two
// API routes from them, so the viewer deploys without the backend. Mirrors
// list_divergences/load_packet in src/statesync/api/server.py: pass-through only.
// The arm is a literal because import.meta.glob needs one; it must match
// SERVED_ARM in eval/arms.py.
const SERVED_ARM = "full";
const bundled = import.meta.glob<Packet>("../../eval/results/packets/full/*.json", {
  eager: true,
  import: "default",
});

export function staticRoute(url: string): Summary[] | Packet | null {
  const { pathname, searchParams } = new URL(url, "http://x");
  const packets = Object.keys(bundled).sort().map((k) => bundled[k]);
  if (pathname === "/api/divergences") {
    const reason = searchParams.get("reason_code");
    return packets
      .filter((p) => !reason || p.reason_code === reason)
      .map((p) => ({
        served_arm: SERVED_ARM,
        payment_id: p.payment_id,
        reason_code: p.reason_code,
        residual_paise: p.residual_paise,
        hypothesis_count: p.hypotheses.length,
      }));
  }
  const id = pathname.replace("/api/divergences/", "");
  return packets.find((p) => p.payment_id === id) ?? null;
}

export function getJson<T>(url: string): Promise<T> {
  if (import.meta.env.MODE === "static") return Promise.resolve(staticRoute(url) as T);
  return fetch(url).then((r) => r.json());
}
