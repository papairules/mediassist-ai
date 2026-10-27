"""Executable evaluation for the bounded conversational analytics matrix."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib.resources import files
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, cast
import uuid

from crestline_agent.cli import (
    DEFAULT_PROFILE,
    DEFAULT_VECTOR_INDEX,
    DEFAULT_WAREHOUSE_ID,
    SYNTHETIC_AS_OF,
    build_demo_only_authorization,
)
from crestline_agent.graph import MediAssistAgent
from crestline_agent.llm import DatabricksIntentClassifier, IntentClassifier
from crestline_retrieval.client import DatabricksBackend
from crestline_retrieval.live_integration import CachingBackend
from crestline_retrieval.service import RetrievalService
from crestline_retrieval.models import AuthorizationContext, EvidenceResult
from crestline_ui.presentation import parse_response

from .fixtures import GoldenFixtureBackend
from .runner import DeterministicClassifier, RecordingService, _source_identifiers


DEFAULT_DATASET = Path("src/crestline_eval/analytics_scenarios.json")
DEFAULT_REPORT_DIRECTORY = Path("build/analytics-evaluation")
DEFAULT_LLM_ENDPOINT = "databricks-qwen3-next-80b-a3b-instruct"


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    repetition: int
    passed: bool
    latency_ms: float
    failures: tuple[str, ...]
    turns: tuple[Mapping[str, Any], ...]


class FailureInjectingService(RecordingService):
    """Inject one bounded read failure for the partial-failure scenario only."""

    def __init__(self, service: RetrievalService, method: str) -> None:
        super().__init__(service)
        self._failure_method = method

    def get_product_inventory(
        self, region: str, sku: str, auth: AuthorizationContext
    ) -> EvidenceResult:
        if self._failure_method == "get_product_inventory":
            raise RuntimeError("synthetic integration failure")
        return super().get_product_inventory(region, sku, auth)


def load_dataset(path: Path | None = None) -> Mapping[str, Any]:
    resource = path or DEFAULT_DATASET
    if path is None:
        payload = json.loads(
            files("crestline_eval").joinpath("analytics_scenarios.json").read_text(
                encoding="utf-8"
            )
        )
    else:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("analytics dataset schema_version must be 1.0")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 34:
        raise ValueError("analytics dataset must contain exactly 34 scenarios")
    return payload


def _check_scenario(
    scenario: Mapping[str, Any],
    *,
    final_intent: str,
    final_response: str,
    final_calls: list[Any],
) -> list[str]:
    failures: list[str] = []
    expected_intent = scenario.get("expected_intent")
    if expected_intent and final_intent != expected_intent:
        failures.append(f"intent expected {expected_intent!r}, found {final_intent!r}")
    lower_response = final_response.lower()
    for term in scenario.get("expected_terms", []):
        if str(term).lower() not in lower_response:
            failures.append(f"response missing {term!r}")
    for term in scenario.get("forbidden_terms", []):
        if str(term).lower() in lower_response:
            failures.append(f"forbidden response text present: {term!r}")
    methods = [call.method for call in final_calls]
    if final_intent in {"greeting", "capability", "unsupported", "clarification"} and methods:
        failures.append(f"static/clarification response unexpectedly called tools: {methods!r}")
    view = parse_response(final_response)
    if not view.display_text.strip():
        failures.append("presentation parser produced an empty display response")
    if final_intent in {"order", "backorder", "inventory", "reporting", "resolution"}:
        if methods and not view.text.strip():
            failures.append("presentation parser lost the authoritative response")
    return failures


def _run_one(
    scenario: Mapping[str, Any],
    *,
    repetition: int,
    service: RecordingService,
    classifier: IntentClassifier,
) -> ScenarioResult:
    started = perf_counter()
    agent = MediAssistAgent()
    thread_id = f"analytics-{scenario['id']}-{repetition}-{uuid.uuid4().hex}"
    reports: list[Mapping[str, Any]] = []
    final_calls: list[Any] = []
    final_intent = ""
    final_response = ""
    for raw_question in scenario["turns"]:
        question = str(raw_question)
        offset = len(service.calls)
        auth = build_demo_only_authorization(str(scenario["scope"]))
        reply = agent.invoke(
            question,
            thread_id=thread_id,
            service=cast(RetrievalService, service),
            authorization=auth,
            classifier=classifier,
            as_of=SYNTHETIC_AS_OF,
        )
        calls = service.calls[offset:]
        final_calls = calls
        final_intent = reply.intent
        final_response = reply.text
        parsed = parse_response(reply.text)
        reports.append(
            {
                "question": question,
                "intent": reply.intent,
                "response": reply.text,
                "display_text": parsed.display_text,
                "methods": [call.method for call in calls],
                "source_identifiers": _source_identifiers(calls),
            }
        )
    failures = _check_scenario(
        scenario,
        final_intent=final_intent,
        final_response=final_response,
        final_calls=final_calls,
    )
    return ScenarioResult(
        scenario_id=str(scenario["id"]),
        repetition=repetition,
        passed=not failures,
        latency_ms=round((perf_counter() - started) * 1000, 3),
        failures=tuple(failures),
        turns=tuple(reports),
    )


def run_analytics(
    *,
    service: RetrievalService,
    classifier: IntentClassifier,
    dataset: Mapping[str, Any],
    repetitions: int = 1,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    mode: str = "local-fixture",
) -> tuple[int, Mapping[str, Any]]:
    base_service = service._service if isinstance(service, RecordingService) else service
    results: list[ScenarioResult] = []
    for raw in dataset["scenarios"]:
        scenario = cast(Mapping[str, Any], raw)
        count = repetitions if scenario.get("category") in {"resolution", "security", "context"} else 1
        for repetition in range(1, count + 1):
            fault = scenario.get("fault_method")
            recorder: RecordingService = (
                FailureInjectingService(base_service, str(fault))
                if fault
                else RecordingService(base_service)
            )
            results.append(
                _run_one(
                    scenario,
                    repetition=repetition,
                    service=recorder,
                    classifier=classifier,
                )
            )
    payload = {
        "report_version": 1,
        "dataset_schema_version": dataset["schema_version"],
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "passed": sum(result.passed for result in results),
            "failed": sum(not result.passed for result in results),
        },
        "results": [
            {
                "scenario_id": result.scenario_id,
                "repetition": result.repetition,
                "passed": result.passed,
                "latency_ms": result.latency_ms,
                "failures": list(result.failures),
                "turns": list(result.turns),
            }
            for result in results
        ],
    }
    report_directory.mkdir(parents=True, exist_ok=True)
    (report_directory / "analytics_evaluation.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# MediAssist AI Analytics Evaluation",
        "",
        f"- Mode: `{mode}`",
        f"- Passed: **{payload['summary']['passed']}**",
        f"- Failed: **{payload['summary']['failed']}**",
        "",
        "| Scenario | Repeat | Result | Latency ms | Failure |",
        "|---|---:|---|---:|---|",
    ]
    for result in results:
        lines.append(
            f"| `{result.scenario_id}` | {result.repetition} | "
            f"{'PASS' if result.passed else 'FAIL'} | {result.latency_ms} | "
            f"{'; '.join(result.failures) or '—'} |"
        )
    (report_directory / "analytics_evaluation.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    failed = int(payload["summary"]["failed"])
    print(
        f"analytics evaluation mode={mode} passed={payload['summary']['passed']} "
        f"failed={failed} total={payload['summary']['total']}"
    )
    return (1 if failed else 0), payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="opt in to read-only Databricks calls")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE_ID)
    parser.add_argument("--vector-index", default=DEFAULT_VECTOR_INDEX)
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    if args.live:
        from databricks.sdk import WorkspaceClient

        workspace = WorkspaceClient(
            profile=args.profile,
            product="mediassist-analytics-evaluation",
            product_version="0.1.0",
        )
        backend = CachingBackend(DatabricksBackend(workspace, args.warehouse_id))
        service: RetrievalService = RetrievalService(backend, vector_index=args.vector_index)
        classifier: IntentClassifier = DatabricksIntentClassifier(workspace, args.llm_endpoint)
        mode = "live-read-only"
    else:
        service = RetrievalService(GoldenFixtureBackend(), vector_index=args.vector_index)
        classifier = DeterministicClassifier()
        mode = "local-fixture"
    code, _ = run_analytics(
        service=service,
        classifier=classifier,
        dataset=dataset,
        repetitions=args.repetitions,
        report_directory=args.report_directory,
        mode=mode,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
