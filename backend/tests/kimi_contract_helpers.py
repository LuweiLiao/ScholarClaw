"""Test-only helpers for Kimi enhanced integration contracts.

These helpers intentionally avoid importing production bridge code so checks stay stable
when executor/command-line wiring moves between modules.
"""

from __future__ import annotations


def argv_has_stage_range(
    argv: list[str],
    from_token: str,
    to_token: str,
) -> bool:
    """True if *argv* contains ``--from-stage <from_token>`` then ``--to-stage <to_token>``.

    Tokens are compared as strings (stage numbers or canonical stage names).
    """
    try:
        i = argv.index("--from-stage")
        j = argv.index("--to-stage")
    except ValueError:
        return False
    if i + 1 >= len(argv) or j + 1 >= len(argv):
        return False
    return argv[i + 1] == from_token and argv[j + 1] == to_token


def expected_node_detail_stage_fields() -> frozenset[str]:
    """Keys the node detail / summary payload is expected to expose for stage range (contract)."""
    return frozenset({"stage_from", "stage_to", "stage_labels", "stage_count"})
