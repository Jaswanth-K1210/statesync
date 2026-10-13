import { describe, expect, it } from "vitest";
import { staticRoute } from "../src/api";
import type { Packet, Summary } from "../src/types";

// The static build answers from the committed packets; these read the real files.
describe("static routes", () => {
  it("lists every bundled packet and resolves each one verbatim", () => {
    const rows = staticRoute("/api/divergences") as Summary[];
    expect(rows.length).toBe(7);
    for (const row of rows) {
      const p = staticRoute(`/api/divergences/${row.payment_id}`) as Packet;
      expect(p.payment_id).toBe(row.payment_id);
      expect(row.hypothesis_count).toBe(p.hypotheses.length);
      expect(row.served_arm).toBe("full");
    }
  });

  it("filters by reason_code and 404s to null", () => {
    const rows = staticRoute("/api/divergences?reason_code=fee_schedule_unknown") as Summary[];
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.reason_code === "fee_schedule_unknown")).toBe(true);
    expect(staticRoute("/api/divergences/pay_nope")).toBeNull();
  });
});
