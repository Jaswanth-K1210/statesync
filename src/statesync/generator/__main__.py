"""CLI: emit a seeded batch as canonical JSON, or just its digest.

    python -m statesync.generator --seed 20260905 --n 500
    python -m statesync.generator --seed 20260905 --n 500 --digest

Writes bytes straight to stdout so `make verify` can diff two runs without a
serialisation step of its own getting in the way.
"""

from __future__ import annotations

import argparse
import sys

from statesync.config import SEED
from statesync.generator.synthetic import generate_batch
from statesync.ledger.canonical import canonical


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="statesync.generator")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--digest", action="store_true", help="print only the SHA-256")
    args = parser.parse_args(argv)

    batch = generate_batch(seed=args.seed, n=args.n)
    payload = batch.digest().encode("ascii") if args.digest else canonical(batch.as_event())
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
