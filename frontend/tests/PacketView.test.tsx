import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PacketView } from "../src/PacketView";
import { rupees } from "../src/money";
import type { Packet } from "../src/types";

// Rejected hypotheses are the point of this screen. These tests exist to stop
// anyone "tidying" them behind a toggle.

function hypothesis(over: Partial<Packet["hypotheses"][0]> = {}) {
  return {
    label: "H1",
    pass_no: 1,
    components: [{ name: "mdr", amount_paise: 800, cites: "pay_1", rate_bps: 0 }],
    sum_paise: 800,
    residual_paise: 1140,
    matched_residual: false,
    arithmetic_shown: "800 = 800",
    citations: [{ artifact: "pay_1", resolves: true }],
    verdict: "ARITHMETIC_FAILED" as const,
    rejection_reason: "components sum to 800 paise, residual is 1140 (off by 340)",
    ...over,
  };
}

function packet(over: Partial<Packet> = {}): Packet {
  return {
    payment_id: "pay_hc14",
    order_id: "order_hc14",
    klass: "amount_mismatch",
    order_total_paise: 400000,
    settled_paise: 389420,
    known_components: [
      { name: "fee_and_tax", amount_paise: 9440, source: "payment.fee_paise" },
    ],
    residual_paise: 1140,
    hypotheses: [hypothesis()],
    reason_code: "no_hypothesis_verified",
    suggested_action: "No generated hypothesis verified for the 1140 paise residual.",
    passes_used: 2,
    ...over,
  };
}

describe("packet viewer", () => {
  it("renders every hypothesis, rejected ones included", () => {
    render(
      <PacketView
        packet={packet({
          hypotheses: [
            hypothesis({ label: "H1" }),
            hypothesis({ label: "H2", verdict: "ARTIFACT_MISSING" }),
            hypothesis({ label: "H3", verdict: "RANGE_VIOLATION" }),
          ],
        })}
      />,
    );
    expect(screen.getAllByTestId("hypothesis")).toHaveLength(3);
  });

  it("shows a verdict badge for each hypothesis", () => {
    render(
      <PacketView
        packet={packet({
          hypotheses: [
            hypothesis({ label: "H1", verdict: "ARITHMETIC_FAILED" }),
            hypothesis({ label: "H2", verdict: "RATE_INCONSISTENT" }),
          ],
        })}
      />,
    );
    expect(screen.getByText("ARITHMETIC_FAILED")).toBeDefined();
    expect(screen.getByText("RATE_INCONSISTENT")).toBeDefined();
  });

  it("shows two VERIFIED badges with different breakdowns for an ambiguous case", () => {
    render(
      <PacketView
        packet={packet({
          reason_code: "ambiguous_multiple_verified",
          hypotheses: [
            hypothesis({
              label: "H1",
              verdict: "VERIFIED",
              matched_residual: true,
              components: [
                { name: "instant_settlement", amount_paise: 966, cites: "stl_hard", rate_bps: 0 },
              ],
            }),
            hypothesis({
              label: "H2",
              verdict: "VERIFIED",
              matched_residual: true,
              components: [{ name: "refund", amount_paise: 1000, cites: "rfnd_hard", rate_bps: 0 }],
            }),
          ],
        })}
      />,
    );
    expect(screen.getAllByText("VERIFIED")).toHaveLength(2);
    expect(screen.getByText("instant_settlement")).toBeDefined();
    expect(screen.getByText("refund")).toBeDefined();
  });

  it("renders the arithmetic string as stored rather than recomputing it", () => {
    // A deliberately impossible sum: if the component computed it, this fails.
    render(
      <PacketView
        packet={packet({
          hypotheses: [hypothesis({ arithmetic_shown: "STORED-NOT-COMPUTED" })],
        })}
      />,
    );
    expect(screen.getByText(/STORED-NOT-COMPUTED/)).toBeDefined();
  });

  it("renders the match flag as stored rather than comparing the numbers", () => {
    render(
      <PacketView
        packet={packet({
          // sum != residual, but the verifier said it matched. The UI obeys.
          hypotheses: [hypothesis({ sum_paise: 1, matched_residual: true })],
        })}
      />,
    );
    expect(screen.getByText(/matches residual/)).toBeDefined();
  });

  it("shows the residual prominently", () => {
    render(<PacketView packet={packet()} />);
    expect(screen.getByTestId("residual").textContent).toBe("₹11.40");
  });

  it("labels each known component with its source", () => {
    render(<PacketView packet={packet()} />);
    expect(screen.getByText("payment.fee_paise")).toBeDefined();
  });

  it("marks an unresolved citation", () => {
    const { container } = render(
      <PacketView
        packet={packet({
          hypotheses: [
            hypothesis({ citations: [{ artifact: "rfnd_ghost", resolves: false }] }),
          ],
        })}
      />,
    );
    expect(container.querySelector(".cite.missing")?.textContent).toContain("rfnd_ghost");
  });

  it("shows the reason code and the templated action", () => {
    render(<PacketView packet={packet()} />);
    expect(screen.getByTestId("reason-code").textContent).toBe("no_hypothesis_verified");
    expect(screen.getByText(/No generated hypothesis verified/)).toBeDefined();
  });

  it("says so when nothing could be priced", () => {
    render(
      <PacketView
        packet={packet({
          known_components: [],
          hypotheses: [],
          reason_code: "fee_schedule_unknown",
          needed_config: "fee_schedule.rates.crypto_voucher",
        })}
      />,
    );
    expect(screen.getByText(/Nothing could be priced/)).toBeDefined();
    expect(screen.getByText(/crypto_voucher/)).toBeDefined();
  });
});

describe("money", () => {
  it("renders paise as rupees with two decimals", () => {
    expect(rupees(1140)).toBe("₹11.40");
    expect(rupees(400000)).toBe("₹4,000.00");
    expect(rupees(1)).toBe("₹0.01");
  });

  it("handles negatives", () => {
    expect(rupees(-250000)).toBe("-₹2,500.00");
  });
});
