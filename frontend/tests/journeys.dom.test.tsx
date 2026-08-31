import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";

// The same two journeys as tests/e2e/journeys.spec.ts, driven through the real
// component tree with the network stubbed.
//
// The Playwright specs are the real thing and run on a developer machine. They
// could not be executed in the environment this was built in — the browser
// binary fails to spawn under its sandbox — so these exist so the journeys are
// not merely *written*. They exercise filter, selection, fetch wiring and
// render; what they cannot cover is layout and real browser behaviour.

const packets: Record<string, unknown> = {
  pay_hc14: {
    payment_id: "pay_hc14", order_id: "order_hc14", klass: "amount_mismatch",
    order_total_paise: 400000, settled_paise: 389420,
    known_components: [{ name: "fee_and_tax", amount_paise: 9440, source: "payment.fee_paise" }],
    residual_paise: 1140,
    hypotheses: [1, 2, 3, 4, 5].map((n) => ({
      label: `H${n}`, pass_no: 1,
      components: [{ name: "x", amount_paise: 1079, cites: "pay_hc14", rate_bps: 0 }],
      sum_paise: 1079, residual_paise: 1140, matched_residual: false,
      arithmetic_shown: "1079 = 1079",
      citations: [{ artifact: "pay_hc14", resolves: true }],
      verdict: "ARITHMETIC_FAILED", rejection_reason: "off by 61",
    })),
    reason_code: "no_hypothesis_verified",
    suggested_action: "No generated hypothesis verified for the 1140 paise residual.",
    passes_used: 2,
  },
  pay_hc13: {
    payment_id: "pay_hc13", order_id: "order_hc13", klass: "amount_mismatch",
    order_total_paise: 400000, settled_paise: 389420,
    known_components: [{ name: "fee_and_tax", amount_paise: 9440, source: "payment.fee_paise" }],
    residual_paise: 1140,
    hypotheses: [
      {
        label: "H1", pass_no: 1,
        components: [{ name: "instant_settlement", amount_paise: 966, cites: "stl_hard", rate_bps: 0 }],
        sum_paise: 1140, residual_paise: 1140, matched_residual: true,
        arithmetic_shown: "966 + 174 = 1140",
        citations: [{ artifact: "stl_hard", resolves: true }],
        verdict: "VERIFIED", rejection_reason: "",
      },
      {
        label: "H2", pass_no: 1,
        components: [{ name: "refund", amount_paise: 1000, cites: "rfnd_hard", rate_bps: 0 }],
        sum_paise: 1140, residual_paise: 1140, matched_residual: true,
        arithmetic_shown: "1000 + 140 = 1140",
        citations: [{ artifact: "rfnd_hard", resolves: true }],
        verdict: "VERIFIED", rejection_reason: "",
      },
    ],
    reason_code: "ambiguous_multiple_verified",
    suggested_action: "Two or more hypotheses reconcile the 1140 paise residual exactly.",
    passes_used: 1,
  },
};

const summaries = [
  { payment_id: "pay_hc13", reason_code: "ambiguous_multiple_verified", residual_paise: 1140, hypothesis_count: 2 },
  { payment_id: "pay_hc14", reason_code: "no_hypothesis_verified", residual_paise: 1140, hypothesis_count: 5 },
];

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = String(url);
    if (path.includes("?reason_code=")) {
      const reason = path.split("=")[1];
      return Promise.resolve({ json: () => Promise.resolve(summaries.filter((s) => s.reason_code === reason)) });
    }
    if (path.endsWith("/api/divergences")) {
      return Promise.resolve({ json: () => Promise.resolve(summaries) });
    }
    const id = path.split("/").pop()!;
    return Promise.resolve({ json: () => Promise.resolve(packets[id]) });
  }));
});

describe("journey: ops reviews a refusal", () => {
  it("filters to no_hypothesis_verified and sees every candidate with its reason", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "no hypothesis verified" })).click();

    await waitFor(() => expect(screen.getByTestId("reason-code").textContent)
      .toBe("no_hypothesis_verified"));
    expect(screen.getByTestId("residual").textContent).toBe("₹11.40");
    expect(screen.getAllByTestId("hypothesis")).toHaveLength(5);
    expect(screen.getAllByText(/off by 61/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("ARITHMETIC_FAILED").length).toBe(5);
  });
});

describe("journey: ops distinguishes an ambiguous case", () => {
  it("shows exactly two VERIFIED badges with different breakdowns", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "ambiguous multiple verified" })).click();

    await waitFor(() => expect(screen.getByTestId("reason-code").textContent)
      .toBe("ambiguous_multiple_verified"));
    expect(screen.getAllByText("VERIFIED")).toHaveLength(2);
    expect(screen.getByText("instant_settlement")).toBeDefined();
    expect(screen.getByText("refund")).toBeDefined();
  });
});
