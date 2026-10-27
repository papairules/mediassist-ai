from pathlib import Path

from crestline_eval.analytics_runner import load_dataset, run_analytics
from crestline_eval.fixtures import GoldenFixtureBackend
from crestline_eval.runner import DeterministicClassifier
from crestline_retrieval.service import RetrievalService


def test_analytics_dataset_has_exactly_34_scenarios() -> None:
    dataset = load_dataset()
    assert len(dataset["scenarios"]) == 34
    assert len({scenario["id"] for scenario in dataset["scenarios"]}) == 34


def test_analytics_runner_executes_real_local_agent_and_presents_responses(tmp_path: Path) -> None:
    dataset = load_dataset()
    code, report = run_analytics(
        service=RetrievalService(GoldenFixtureBackend()),
        classifier=DeterministicClassifier(),
        dataset=dataset,
        repetitions=1,
        report_directory=tmp_path,
    )
    assert code == 0
    assert report["summary"] == {"total": 34, "passed": 34, "failed": 0}
    assert (tmp_path / "analytics_evaluation.json").exists()
    assert (tmp_path / "analytics_evaluation.md").exists()
