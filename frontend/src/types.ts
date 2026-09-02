// Mirrors the stored packet exactly. Every field is rendered as served; the UI
// computes no sums, no matches and no verdicts. That is where the seam bug in
// docs/seam_bugs.json would recur.

export type Verdict =
  | "VERIFIED"
  | "ARITHMETIC_FAILED"
  | "ARTIFACT_MISSING"
  | "RANGE_VIOLATION"
  | "RATE_INCONSISTENT";

export interface Component {
  name: string;
  amount_paise: number;
  cites: string;
  rate_bps: number;
}

export interface Citation {
  artifact: string;
  resolves: boolean;
}

export interface Hypothesis {
  label: string;
  pass_no: number;
  components: Component[];
  sum_paise: number;
  residual_paise: number;
  matched_residual: boolean;
  arithmetic_shown: string;
  citations: Citation[];
  verdict: Verdict;
  rejection_reason: string;
}

export interface KnownComponent {
  name: string;
  amount_paise: number;
  source: string;
}

export interface Packet {
  payment_id: string;
  order_id: string;
  klass: string;
  order_total_paise: number;
  settled_paise: number;
  known_components: KnownComponent[];
  residual_paise: number;
  hypotheses: Hypothesis[];
  reason_code: string;
  suggested_action: string;
  passes_used: number;
  needed_config?: string;
}

export interface Summary {
  served_arm?: string;
  payment_id: string;
  reason_code: string;
  residual_paise: number;
  hypothesis_count: number;
}
