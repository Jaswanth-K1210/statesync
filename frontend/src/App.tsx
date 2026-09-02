import { useEffect, useState } from "react";
import type { Packet, Summary } from "./types";
import { PacketView } from "./PacketView";
import { rupees } from "./money";

const REASONS = [
  "all",
  "no_hypothesis_verified",
  "ambiguous_multiple_verified",
  "fee_schedule_unknown",
];

export function App() {
  const [rows, setRows] = useState<Summary[]>([]);
  const [arm, setArm] = useState<string>("");
  const [reason, setReason] = useState("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);

  useEffect(() => {
    const query = reason === "all" ? "" : `?reason_code=${reason}`;
    fetch(`/api/divergences${query}`)
      .then((r) => r.json())
      .then((data: Summary[]) => {
        setRows(data);
        if (data.length > 0 && data[0].served_arm) setArm(data[0].served_arm);
        setSelected(data.length > 0 ? data[0].payment_id : null);
      });
  }, [reason]);

  useEffect(() => {
    if (!selected) {
      setPacket(null);
      return;
    }
    fetch(`/api/divergences/${selected}`)
      .then((r) => r.json())
      .then(setPacket);
  }, [selected]);

  return (
    <div className="layout">
      <nav className="list">
        <h1>
          Escalations{arm ? <span className="arm"> · arm: {arm}</span> : null}
        </h1>
        <div className="filters">
          {REASONS.map((r) => (
            <button key={r} aria-pressed={reason === r} onClick={() => setReason(r)}>
              {r === "all" ? "all" : r.replace(/_/g, " ")}
            </button>
          ))}
        </div>
        {rows.map((row) => (
          <button
            key={row.payment_id}
            className="case"
            aria-current={selected === row.payment_id}
            onClick={() => setSelected(row.payment_id)}
          >
            <span className="id">{row.payment_id}</span>
            <span className="amt">{rupees(row.residual_paise)}</span>
            <span className="reason">
              {row.reason_code.replace(/_/g, " ")} · {row.hypothesis_count} hypotheses
            </span>
          </button>
        ))}
      </nav>
      {packet ? <PacketView packet={packet} /> : <div className="empty">No escalation selected.</div>}
    </div>
  );
}
