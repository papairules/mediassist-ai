from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, cast

from crestline_eval.fixtures import GoldenFixtureBackend
from crestline_eval.runner import (
    DeterministicClassifier,
    _parser,
    _semantic_fact_equal,
    load_golden_dataset,
    run_evaluation,
)
from crestline_retrieval.service import RetrievalService


REQUIRED_SCENARIOS = {
    "order_1001_visibility",
    "supplier_eta_not_delivery_promise",
    "contextual_candidate_discovery",
    "h_e101_brand_lock",
    "h_e102_amendment_without_order_context",
    "h_e102_amendment_with_order_context",
    "latex_candidate_emergency_wording",
    "order_1003_unknown_eta",
    "east_cannot_view_west_order",
    "west_cannot_view_east_order",
    "explicit_hospital_invalidates_stale_order",
    "new_conversation_session_isolation",
}


def _service() -> RetrievalService:
    return RetrievalService(GoldenFixtureBackend())


def test_numeric_facts_accept_databricks_string_scalars() -> None:
    assert _semantic_fact_equal("100", 100)
    assert _semantic_fact_equal("60", 60)
    assert _semantic_fact_equal("40", 40)
    assert _semantic_fact_equal("45", 45)


def test_numeric_fact_comparison_rejects_invalid_or_incorrect_values() -> None:
    assert not _semantic_fact_equal("not-a-number", 40)
    assert not _semantic_fact_equal("40.5", 40)
    assert not _semantic_fact_equal("41", 40)
    assert not _semantic_fact_equal(True, 1)
    assert not _semantic_fact_equal(False, 0)


def test_nonnumeric_fact_comparison_remains_strict() -> None:
    assert _semantic_fact_equal("BLOCKED", "BLOCKED")
    assert not _semantic_fact_equal("blocked", "BLOCKED")
    assert _semantic_fact_equal("40", "40")
    assert _semantic_fact_equal(True, True)


def test_golden_dataset_covers_required_safety_and_context_scenarios() -> None:
    dataset = load_golden_dataset()
    scenarios = cast(list[dict[str, Any]], dataset["scenarios"])

    assert {scenario["id"] for scenario in scenarios} == REQUIRED_SCENARIOS
    assert {scenario["scope"] for scenario in scenarios} == {"east", "west"}
    assert any(len(scenario["turns"]) >= 3 for scenario in scenarios)
    assert any(
        turn.get("new_conversation")
        for scenario in scenarios
        for turn in scenario["turns"]
    )
    serialized = json.dumps(dataset).lower()
    assert "conditional_amendment_requires_order_line_context" in serialized
    assert "current_guide_status_blocks_candidate" in serialized
    assert "confirmed delivery date" in serialized
    assert "clinically approved" in serialized


def test_local_golden_evaluation_passes_three_stable_runs(
    tmp_path: Path,
) -> None:
    exit_code, report = run_evaluation(
        service=_service(),
        classifier=DeterministicClassifier(),
        dataset=load_golden_dataset(),
        mode="local-fixture",
        repetitions=3,
        report_directory=tmp_path,
    )

    assert exit_code == 0
    assert report["summary"] == {"total": 36, "passed": 36, "failed": 0}
    assert (tmp_path / "golden_evaluation.json").is_file()
    markdown = (tmp_path / "golden_evaluation.md").read_text(encoding="utf-8")
    assert "MediAssist AI Golden Evaluation" in markdown
    assert "| FAIL |" not in markdown
    results = cast(list[dict[str, Any]], report["results"])
    latex = next(
        result
        for result in results
        if result["scenario_id"] == "latex_candidate_emergency_wording"
    )
    assert "SUB_GUIDE_2026_V2_CURRENT" in latex["source_identifiers"]
    assert "IFU_BR-IV22-BLU-LX" in latex["source_identifiers"]


def test_golden_evaluation_fails_closed_on_business_fact_regression(
    tmp_path: Path,
) -> None:
    dataset = deepcopy(load_golden_dataset())
    scenarios = cast(list[dict[str, Any]], dataset["scenarios"])
    order = next(item for item in scenarios if item["id"] == "order_1001_visibility")
    order["turns"][0]["expected"]["facts"][0]["equals"] = 999

    exit_code, report = run_evaluation(
        service=_service(),
        classifier=DeterministicClassifier(),
        dataset=dataset,
        mode="local-fixture",
        repetitions=1,
        report_directory=tmp_path,
    )

    assert exit_code == 1
    assert report["summary"]["failed"] == 1
    saved = json.loads(
        (tmp_path / "golden_evaluation.json").read_text(encoding="utf-8")
    )
    failure = next(
        result
        for result in saved["results"]
        if result["scenario_id"] == "order_1001_visibility"
    )
    assert "quantity_ordered expected 999" in failure["failures"][0]


def test_live_mode_is_explicitly_opt_in() -> None:
    local = _parser().parse_args([])
    live = _parser().parse_args(["--live"])

    assert local.live is False
    assert live.live is True
