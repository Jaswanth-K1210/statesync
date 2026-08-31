"""Two read-only endpoints over the committed escalation packets.

    GET /api/divergences                  escalated only; ?reason_code= filters
    GET /api/divergences/{payment_id}     one full packet

**Serves one arm.** `SERVED_ARM` names which, once, so the screen and the
README's headline cannot drift apart — the packets directory is namespaced by
arm precisely because they did.

**Serves files, not a database.** The packets in `eval/results/packets/` are
the committed output of the eval, so this needs no Postgres, no Redis and no
run to be in flight. `make demo` does not depend on it.

**Nothing is computed here.** Every field is passed through as stored. The API
layer is exactly where the seam bug in `docs/seam_bugs.json` would recur — a
residual recomputed, a sum re-derived, a verdict re-decided — so it does none
of those things, and a consistency test asserts the JSON matches the packet,
the CSV and the ledger.

Standard library only. Two read-only endpoints over static JSON do not justify
adding a web framework and its dependency tree to a project whose primary demo
runs in a terminal.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eval.arms import PACKETS_DIR, SERVED_ARM

__all__ = ["PacketHandler", "list_divergences", "load_packet", "serve"]


def load_packet(payment_id: str, packets_dir: Path | None = None) -> dict[str, Any] | None:
    """One packet, verbatim. None if there is no such escalation."""
    directory = packets_dir or (PACKETS_DIR / SERVED_ARM)
    # Guard against traversal: the id is a filename component, never a path.
    if "/" in payment_id or ".." in payment_id:
        return None
    path = directory / f"{payment_id}.json"
    if not path.exists():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def list_divergences(
    reason_code: str | None = None, packets_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Summaries for the list pane, sorted for a stable order.

    Carries only what the list renders — the full packet is a second request,
    so the summary can never drift into being a second source of truth for
    fields the detail view also shows.
    """
    directory = packets_dir or (PACKETS_DIR / SERVED_ARM)
    if not directory.exists():
        return []

    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        packet = json.loads(path.read_text(encoding="utf-8"))
        if reason_code and packet["reason_code"] != reason_code:
            continue
        rows.append({
            "payment_id": packet["payment_id"],
            "reason_code": packet["reason_code"],
            "residual_paise": packet["residual_paise"],
            "hypothesis_count": len(packet["hypotheses"]),
        })
    return rows


class PacketHandler(BaseHTTPRequestHandler):
    packets_dir: Path | None = None

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.strip("/").split("/") if p]

        if parts == ["api", "divergences"]:
            reason = parse_qs(parsed.query).get("reason_code", [None])[0]
            self._send(200, list_divergences(reason, self.packets_dir))
            return

        if len(parts) == 3 and parts[:2] == ["api", "divergences"]:
            packet = load_packet(parts[2], self.packets_dir)
            if packet is None:
                self._send(404, {"error": "no escalation for that payment"})
                return
            self._send(200, packet)
            return

        self._send(404, {"error": "not found"})

    def log_message(self, *_: Any) -> None:
        """Quiet. The demo runs in a terminal and request logs are noise."""


def serve(port: int = 8787, packets_dir: Path | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (PacketHandler,), {"packets_dir": packets_dir})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


if __name__ == "__main__":
    server = serve()
    print(f"packets API on http://127.0.0.1:{server.server_address[1]}/api/divergences")  # noqa: T201
    server.serve_forever()
