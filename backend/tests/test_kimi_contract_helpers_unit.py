"""Sanity checks for ``kimi_contract_helpers`` (GREEN)."""

from __future__ import annotations

from kimi_contract_helpers import argv_has_stage_range, expected_node_detail_stage_fields


def test_argv_has_stage_range_positive():
    argv = ["python", "-m", "researchclaw", "run", "--from-stage", "S3", "--to-stage", "S7", "--auto-approve"]
    assert argv_has_stage_range(argv, "S3", "S7")


def test_argv_has_stage_range_negative():
    assert not argv_has_stage_range(["--foo"], "S1", "S2")


def test_expected_detail_fields_non_empty():
    assert "stage_from" in expected_node_detail_stage_fields()
