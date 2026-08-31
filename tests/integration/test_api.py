"""The two read-only endpoints.

The API layer is exactly where the seam bug recurs — a residual recomputed, a
sum re-derived, a verdict re-decided. It does none of those things, and
`test_reporting_consistency.py` asserts the JSON matches the packet, the CSV
and the ledger.
"""

import json
import urllib.request

import pytest

from statesync.api.server import list_divergences, load_packet, serve

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api():
    server = serve(port=0)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode())


def test_the_list_returns_escalations(api):
    status, rows = get(api, "/api/divergences")
    assert status == 200 and len(rows) > 0


def test_the_list_can_filter_by_reason_code(api):
    _, rows = get(api, "/api/divergences?reason_code=ambiguous_multiple_verified")
    assert rows and all(r["reason_code"] == "ambiguous_multiple_verified" for r in rows)


def test_a_filter_matching_nothing_returns_an_empty_list(api):
    # stuck_repair is a real reason code that no escalation in this batch has.
    _, rows = get(api, "/api/divergences?reason_code=stuck_repair")
    assert rows == []


def test_one_packet_is_served_whole(api):
    _, packet = get(api, "/api/divergences/pay_hc14")
    assert packet["payment_id"] == "pay_hc14"
    assert len(packet["hypotheses"]) >= 5


def test_rejected_hypotheses_are_present_in_the_response(api):
    _, packet = get(api, "/api/divergences/pay_hc14")
    assert not [h for h in packet["hypotheses"] if h["verdict"] == "VERIFIED"]
    assert all(h["rejection_reason"] for h in packet["hypotheses"])


def test_the_ambiguous_packet_serves_two_verified(api):
    _, packet = get(api, "/api/divergences/pay_hc13")
    verified = [h for h in packet["hypotheses"] if h["verdict"] == "VERIFIED"]
    assert len(verified) == 2


def test_an_unknown_payment_is_a_404(api):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(api, "/api/divergences/pay_nonexistent")
    assert exc.value.code == 404


def test_path_traversal_is_refused():
    assert load_packet("../../../etc/passwd") is None
    assert load_packet("a/b") is None


def test_the_api_serves_the_packet_verbatim(api):
    """No field is transformed on the way out."""
    _, served = get(api, "/api/divergences/pay_hc13")
    stored = load_packet("pay_hc13")
    assert served == stored


def test_the_list_summary_matches_the_packet_it_summarises(api):
    _, rows = get(api, "/api/divergences")
    for row in rows:
        stored = load_packet(row["payment_id"])
        assert row["residual_paise"] == stored["residual_paise"]
        assert row["reason_code"] == stored["reason_code"]
        assert row["hypothesis_count"] == len(stored["hypotheses"])


def test_the_list_is_ordered_stably(api):
    _, first = get(api, "/api/divergences")
    _, second = get(api, "/api/divergences")
    assert first == second


def test_list_divergences_works_without_a_server():
    assert len(list_divergences()) > 0
