"""The blast-radius cap.

A reconciler that repairs 400 records in one run because of one bad rule is
worse than one that stops at 50 and asks. The cap is the difference between a
bug and an incident.

`max_repairs=0` is a working kill switch that needs no code change.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BlastRadiusCap"]


@dataclass
class BlastRadiusCap:
    max_repairs: int
    used: int = 0
    blocked: int = 0

    def __post_init__(self) -> None:
        if self.max_repairs < 0:
            raise ValueError(f"max_repairs must be >= 0, got {self.max_repairs}")

    def allow(self) -> bool:
        """Consume one repair from the budget. False once it is spent."""
        if self.used >= self.max_repairs:
            self.blocked += 1
            return False
        self.used += 1
        return True
