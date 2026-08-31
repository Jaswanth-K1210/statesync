import type { Hypothesis, Packet } from "./types";
import { rupees } from "./money";

// Every value here is rendered as served. The sum, the match flag and the
// verdict were decided by the verifier and stored; recomputing any of them in
// the UI would make this screen the eighth instance of the pattern in
// docs/seam_bugs.json.

function Citations({ hypothesis }: { hypothesis: Hypothesis }) {
  if (hypothesis.citations.length === 0) return <span className="cite">no citation</span>;
  return (
    <>
      {hypothesis.citations.map((c) => (
        <span key={c.artifact} className={c.resolves ? "cite" : "cite missing"}>
          {c.artifact}{" "}
        </span>
      ))}
    </>
  );
}

function HypothesisCard({ hypothesis }: { hypothesis: Hypothesis }) {
  const verified = hypothesis.verdict === "VERIFIED";
  return (
    <article className={verified ? "hyp verified" : "hyp"} data-testid="hypothesis">
      <header>
        <span className="label">{hypothesis.label}</span>
        <span className="pass">pass {hypothesis.pass_no}</span>
        <span className={`badge ${hypothesis.verdict}`}>{hypothesis.verdict}</span>
      </header>

      <table>
        <tbody>
          {hypothesis.components.map((c, i) => (
            <tr key={`${c.name}-${i}`}>
              <td>{c.name}</td>
              <td className="n">{rupees(c.amount_paise)}</td>
              <td className="src">{c.cites}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="arith">
        {hypothesis.arithmetic_shown}{" "}
        {hypothesis.matched_residual ? (
          <span className="match">✓ matches residual</span>
        ) : (
          <span className="nomatch">✗ does not match residual</span>
        )}
      </div>

      <div className="cite" style={{ marginTop: 6 }}>
        cites: <Citations hypothesis={hypothesis} />
      </div>

      {hypothesis.rejection_reason && <p className="why">{hypothesis.rejection_reason}</p>}
    </article>
  );
}

export function PacketView({ packet }: { packet: Packet }) {
  return (
    <div className="detail">
      <h2>{packet.payment_id}</h2>
      <div className="sub">
        {packet.klass} · order {packet.order_id}
      </div>

      <section className="section">
        <h3>The case</h3>
        <table>
          <tbody>
            <tr>
              <td>Order total</td>
              <td className="n">{rupees(packet.order_total_paise)}</td>
            </tr>
            <tr>
              <td>Settled</td>
              <td className="n">{rupees(packet.settled_paise)}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section className="section">
        <h3>Known components — subtracted deterministically, before any model</h3>
        {packet.known_components.length === 0 ? (
          <p className="why">Nothing could be priced for this payment.</p>
        ) : (
          <table>
            <tbody>
              {packet.known_components.map((c, i) => (
                <tr key={`${c.name}-${i}`}>
                  <td>{c.name}</td>
                  <td className="n">{rupees(c.amount_paise)}</td>
                  <td className="src">{c.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <div className="residual">
          <span className="label">Unexplained residual</span>
          <span className="value" data-testid="residual">
            {rupees(packet.residual_paise)}
          </span>
        </div>
      </section>

      <section className="section">
        <h3>
          Every hypothesis the verifier ruled on — {packet.hypotheses.length} candidate
          {packet.hypotheses.length === 1 ? "" : "s"}, rejected ones included
        </h3>
        {packet.hypotheses.length === 0 ? (
          <p className="why">
            None were generated. {packet.needed_config ? `Needs: ${packet.needed_config}` : ""}
          </p>
        ) : (
          packet.hypotheses.map((h) => <HypothesisCard key={h.label} hypothesis={h} />)
        )}
      </section>

      <section className="section">
        <h3>Outcome</h3>
        <div className="outcome">
          <span className={`code ${packet.reason_code}`} data-testid="reason-code">
            {packet.reason_code}
          </span>
          <p>{packet.suggested_action}</p>
          <div className="templated">
            Populated from a fixed template. No model output produces any figure on this screen.
          </div>
        </div>
      </section>
    </div>
  );
}
