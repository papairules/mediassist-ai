"""Repeatable golden conversational evaluation runner for MediAssist AI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib.resources import files
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence, cast
import uuid

from crestline_agent.cli import (
    DEFAULT_PROFILE,
    DEFAULT_VECTOR_INDEX,
    DEFAULT_WAREHOUSE_ID,
    build_demo_only_authorization,
)
from crestline_agent.graph import MediAssistAgent
from crestline_agent.llm import DatabricksIntentClassifier, IntentClassifier, IntentDecision
from crestline_retrieval.client import DatabricksBackend
from crestline_retrieval.live_integration import CachingBackend
from crestline_retrieval.models import (
    AuthorizationContext,
    DocumentSearchResult,
    EvidenceResult,
    Outcome,
    SubstitutionVerification,
)
from crestline_retrieval.service import RetrievalService

from .fixtures import GoldenFixtureBackend


DEFAULT_LLM_ENDPOINT = "databricks-qwen3-next-80b-a3b-instruct"
DEFAULT_REPORT_DIRECTORY = Path("build/evaluation")


class EvaluationConfigurationError(ValueError):
    """Golden evaluation data is malformed."""


def _semantic_fact_equal(actual: Any, expected: Any) -> bool:
    """Compare facts with numeric coercion only for numeric expectations."""

    expected_is_numeric = isinstance(expected, (int, float, Decimal)) and not isinstance(
        expected, bool
    )
    if not expected_is_numeric:
        return actual == expected
    if isinstance(actual, bool) or not isinstance(actual, (str, int, float, Decimal)):
        return False
    try:
        expected_decimal = Decimal(str(expected))
        actual_decimal = Decimal(str(actual).strip())
    except (InvalidOperation, ValueError):
        return False
    if not expected_decimal.is_finite() or not actual_decimal.is_finite():
        return False
    if isinstance(expected, int) and actual_decimal != actual_decimal.to_integral_value():
        return False
    return actual_decimal == expected_decimal


@dataclass(frozen=True)
class RecordedCall:
    method: str
    arguments: Mapping[str, Any]
    result: Any
    latency_ms: float


class RecordingService:
    """Observe public RetrievalService calls without changing their behavior."""

    def __init__(self, service: RetrievalService) -> None:
        self._service = service
        self.calls: list[RecordedCall] = []

    def _record(
        self,
        method: str,
        arguments: Mapping[str, Any],
        operation: Callable[[], Any],
    ) -> Any:
        started = perf_counter()
        result = operation()
        self.calls.append(
            RecordedCall(method, dict(arguments), result, (perf_counter() - started) * 1000)
        )
        return result

    def get_order_visibility(
        self,
        order_id: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        return cast(
            EvidenceResult,
            self._record(
                "get_order_visibility",
                {"order_id": order_id, "trusted_principal": auth.principal_id},
                lambda: self._service.get_order_visibility(order_id, auth),
            ),
        )

    def get_backorder_evidence(
        self,
        order_id: str,
        auth: AuthorizationContext,
        *,
        line_id: str | None = None,
    ) -> EvidenceResult:
        return cast(
            EvidenceResult,
            self._record(
                "get_backorder_evidence",
                {
                    "order_id": order_id,
                    "line_id": line_id,
                    "trusted_principal": auth.principal_id,
                },
                lambda: self._service.get_backorder_evidence(
                    order_id, auth, line_id=line_id
                ),
            ),
        )

    def get_product_inventory(
        self,
        region: str,
        sku: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        return cast(
            EvidenceResult,
            self._record(
                "get_product_inventory",
                {"region": region, "sku": sku, "trusted_principal": auth.principal_id},
                lambda: self._service.get_product_inventory(region, sku, auth),
            ),
        )

    def search_document_evidence(
        self,
        query_text: str,
        auth: AuthorizationContext,
        *,
        num_results: int = 5,
    ) -> DocumentSearchResult:
        return cast(
            DocumentSearchResult,
            self._record(
                "search_document_evidence",
                {
                    "query_text": query_text,
                    "num_results": num_results,
                    "trusted_principal": auth.principal_id,
                },
                lambda: self._service.search_document_evidence(
                    query_text, auth, num_results=num_results
                ),
            ),
        )

    def get_contract_rules(
        self,
        hospital_id: str,
        auth: AuthorizationContext,
        *,
        as_of: date,
    ) -> EvidenceResult:
        return cast(
            EvidenceResult,
            self._record(
                "get_contract_rules",
                {
                    "hospital_id": hospital_id,
                    "as_of": as_of.isoformat(),
                    "trusted_principal": auth.principal_id,
                },
                lambda: self._service.get_contract_rules(hospital_id, auth, as_of=as_of),
            ),
        )

    def verify_substitution(
        self,
        *,
        requested_sku: str,
        candidate_sku: str,
        hospital_id: str,
        region: str,
        auth: AuthorizationContext,
        as_of: date,
        order_id: str | None = None,
        line_id: str | None = None,
        request_text: str | None = None,
    ) -> SubstitutionVerification:
        arguments = {
            "requested_sku": requested_sku,
            "candidate_sku": candidate_sku,
            "hospital_id": hospital_id,
            "region": region,
            "order_id": order_id,
            "line_id": line_id,
            "trusted_principal": auth.principal_id,
        }
        return cast(
            SubstitutionVerification,
            self._record(
                "verify_substitution",
                arguments,
                lambda: self._service.verify_substitution(
                    requested_sku=requested_sku,
                    candidate_sku=candidate_sku,
                    hospital_id=hospital_id,
                    region=region,
                    auth=auth,
                    as_of=as_of,
                    order_id=order_id,
                    line_id=line_id,
                    request_text=request_text,
                ),
            ),
        )


class DeterministicClassifier:
    """Local fallback: ambiguous prompts require clarification, never invented slots."""

    def classify(self, message: str, safe_context: Mapping[str, str]) -> IntentDecision:
        del message, safe_context
        return IntentDecision("clarification")


def load_golden_dataset(path: Path | None = None) -> Mapping[str, Any]:
    if path is None:
        resource = files("crestline_eval").joinpath("golden_scenarios.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise EvaluationConfigurationError("Golden dataset schema_version must be 1")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise EvaluationConfigurationError("Golden dataset must contain scenarios")
    identifiers: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise EvaluationConfigurationError("Each scenario must be an object")
        identifier = scenario.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise EvaluationConfigurationError("Scenario IDs must be non-empty and unique")
        identifiers.add(identifier)
        if scenario.get("scope") not in {"east", "west", "central"}:
            raise EvaluationConfigurationError(f"{identifier}: unsupported DEMO_ONLY scope")
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not turns:
            raise EvaluationConfigurationError(f"{identifier}: turns are required")
    return payload


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for component in path.split("."):
        if is_dataclass(current):
            current = getattr(current, component)
        elif isinstance(current, Mapping):
            current = current[component]
        elif isinstance(current, (list, tuple)):
            current = current[int(component)]
        else:
            current = getattr(current, component)
    return current


def _walk(value: Any) -> list[tuple[str, Any]]:
    output: list[tuple[str, Any]] = []
    if is_dataclass(value):
        for field in fields(value):
            child = getattr(value, field.name)
            output.append((field.name, child))
            output.extend(_walk(child))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            output.append((str(key), child))
            output.extend(_walk(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            output.extend(_walk(child))
    return output


def _source_identifiers(calls: Sequence[RecordedCall]) -> list[str]:
    identifiers: set[str] = set()
    source_keys = {
        "document_id",
        "source_record_id",
        "substitution_row_id",
        "ifu_fact_id",
        "contract_clause_id",
        "shipment_id",
        "tracking_id",
        "record_id",
        "order_record_id",
        "order_line_record_id",
        "current_supplier_eta_record_id",
        "current_supplier_eta_bronze_record_id",
    }
    for call in calls:
        for key, value in _walk(call.result):
            if key in source_keys and value not in (None, ""):
                identifiers.add(str(value))
    return sorted(identifiers)


def _calls_by_name(calls: Sequence[RecordedCall], name: str) -> list[RecordedCall]:
    return [call for call in calls if call.method == name]


def _evaluate_turn(
    expected: Mapping[str, Any],
    *,
    intent: str,
    response: str,
    calls: Sequence[RecordedCall],
) -> list[str]:
    failures: list[str] = []
    expected_intent = expected.get("intent")
    if expected_intent is not None and intent != expected_intent:
        failures.append(f"intent expected {expected_intent!r}, found {intent!r}")
    allowed_intents = expected.get("intent_any_of")
    if isinstance(allowed_intents, list) and intent not in allowed_intents:
        failures.append(f"intent expected one of {allowed_intents!r}, found {intent!r}")
    methods = [call.method for call in calls]
    if "methods" in expected and methods != expected["methods"]:
        failures.append(f"retrieval methods expected {expected['methods']!r}, found {methods!r}")

    expected_outcome = expected.get("outcome")
    if isinstance(expected_outcome, str):
        if not response.startswith(expected_outcome):
            failures.append(f"response did not start with {expected_outcome}")
        verifications = _calls_by_name(calls, "verify_substitution")
        if verifications and expected_outcome in {item.value for item in Outcome}:
            actual = verifications[-1].result.outcome.value
            if actual != expected_outcome:
                failures.append(f"structured outcome expected {expected_outcome}, found {actual}")

    for text in expected.get("response_contains", []):
        if str(text).lower() not in response.lower():
            failures.append(f"response missing required text {text!r}")
    response_alternatives = expected.get("response_contains_any")
    if isinstance(response_alternatives, list) and not any(
        str(text).lower() in response.lower() for text in response_alternatives
    ):
        failures.append(
            f"response missing every allowed text alternative {response_alternatives!r}"
        )
    for claim in expected.get("forbidden_claims", []):
        if str(claim).lower() in response.lower():
            failures.append(f"unsafe or incorrect claim present: {claim!r}")

    for fact in expected.get("facts", []):
        named = _calls_by_name(calls, str(fact["call"]))
        if not named:
            failures.append(f"fact check has no call {fact['call']}")
            continue
        try:
            actual = _resolve_path(named[-1], str(fact["path"]))
        except (AttributeError, KeyError, IndexError, ValueError) as exc:
            failures.append(f"cannot resolve {fact['path']}: {exc}")
            continue
        expected_value = fact.get("equals")
        if not _semantic_fact_equal(actual, expected_value):
            failures.append(
                f"{fact['path']} expected {expected_value!r}, found {actual!r}"
            )

    for method, arguments in expected.get("call_arguments", {}).items():
        named = _calls_by_name(calls, str(method))
        if not named:
            failures.append(f"argument check has no call {method}")
            continue
        for key, wanted in arguments.items():
            actual = named[-1].arguments.get(key)
            if actual != wanted:
                failures.append(f"{method}.{key} expected {wanted!r}, found {actual!r}")

    expected_reasons = set(expected.get("reason_codes", []))
    if expected_reasons:
        verifications = _calls_by_name(calls, "verify_substitution")
        if not verifications:
            failures.append("reason-code check has no substitution verification")
        else:
            actual_reasons = set(verifications[-1].result.reason_codes)
            missing = expected_reasons - actual_reasons
            if missing:
                failures.append(f"missing reason codes: {sorted(missing)}")

    expected_candidates = expected.get("candidate_outcomes", {})
    if expected_candidates:
        actual_candidates = {
            call.result.candidate_sku: call.result.outcome.value
            for call in _calls_by_name(calls, "verify_substitution")
        }
        if actual_candidates != expected_candidates:
            failures.append(
                f"candidate outcomes expected {expected_candidates!r}, found {actual_candidates!r}"
            )

    actual_sources = _source_identifiers(calls)
    for identifier in expected.get("source_ids", []):
        if identifier not in actual_sources:
            failures.append(f"structured evidence missing source identifier {identifier}")
        if identifier not in response:
            failures.append(f"response citation missing source identifier {identifier}")
    return failures


def _stability_signature(result: Mapping[str, Any]) -> str:
    turns = [
        {
            "intent": turn["intent"],
            "methods": turn["methods"],
            "source_identifiers": turn["source_identifiers"],
            "outcome": turn["outcome"],
        }
        for turn in result["turns"]
    ]
    return json.dumps(turns, sort_keys=True, separators=(",", ":"))


def _run_scenario(
    scenario: Mapping[str, Any],
    *,
    repetition: int,
    service: RecordingService,
    classifier: IntentClassifier,
    as_of: date,
) -> dict[str, Any]:
    agent = MediAssistAgent()
    thread_id = f"golden-{scenario['id']}-{repetition}-{uuid.uuid4().hex}"
    turns_report: list[dict[str, Any]] = []
    scenario_started = perf_counter()
    for turn_number, raw_turn in enumerate(scenario["turns"], start=1):
        turn = cast(Mapping[str, Any], raw_turn)
        if turn.get("new_conversation"):
            agent = MediAssistAgent()
            thread_id = f"golden-{scenario['id']}-{repetition}-reset-{uuid.uuid4().hex}"
        question = str(turn["question"])
        call_offset = len(service.calls)
        started = perf_counter()
        # Authorization is reconstructed from trusted evaluator configuration on
        # every turn. Question text and checkpoint state never define scope.
        authorization = build_demo_only_authorization(str(scenario["scope"]))
        reply = agent.invoke(
            question,
            thread_id=thread_id,
            service=cast(RetrievalService, service),
            authorization=authorization,
            classifier=classifier,
            as_of=as_of,
        )
        elapsed_ms = (perf_counter() - started) * 1000
        turn_calls = service.calls[call_offset:]
        expected = cast(Mapping[str, Any], turn.get("expected", {}))
        failures = _evaluate_turn(
            expected,
            intent=reply.intent,
            response=reply.text,
            calls=turn_calls,
        )
        outcome = next(
            (
                candidate
                for candidate in (
                    "ELIGIBLE_FOR_HUMAN_REVIEW",
                    "INSUFFICIENT_EVIDENCE",
                    "ACCESS_DENIED",
                    "BLOCKED",
                    "CLARIFICATION_REQUIRED",
                )
                if reply.text.startswith(candidate)
            ),
            None,
        )
        turns_report.append(
            {
                "turn": turn_number,
                "question": question,
                "intent": reply.intent,
                "outcome": outcome,
                "passed": not failures,
                "latency_ms": round(elapsed_ms, 3),
                "methods": [call.method for call in turn_calls],
                "source_identifiers": _source_identifiers(turn_calls),
                "failures": failures,
            }
        )
    failures = [
        f"turn {turn['turn']}: {failure}"
        for turn in turns_report
        for failure in turn["failures"]
    ]
    return {
        "scenario_id": scenario["id"],
        "description": scenario["description"],
        "scope": scenario["scope"],
        "critical": bool(scenario.get("critical")),
        "repetition": repetition,
        "passed": not failures,
        "latency_ms": round((perf_counter() - scenario_started) * 1000, 3),
        "source_identifiers": sorted(
            {
                identifier
                for turn in turns_report
                for identifier in turn["source_identifiers"]
            }
        ),
        "failures": failures,
        "turns": turns_report,
    }


def _write_reports(report: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "golden_evaluation.json"
    markdown_path = directory / "golden_evaluation.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = cast(Mapping[str, Any], report["summary"])
    lines = [
        "# MediAssist AI Golden Evaluation",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Dataset schema: `{report['dataset_schema_version']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Runs: **{summary['total']}**",
        f"- Passed: **{summary['passed']}**",
        f"- Failed: **{summary['failed']}**",
        "",
        "| Scenario | Repeat | Scope | Result | Latency ms | Sources | Failure |",
        "|---|---:|---|---|---:|---|---|",
    ]
    for result in report["results"]:
        failure = "; ".join(result["failures"]) or "—"
        sources = ", ".join(result["source_identifiers"]) or "—"
        lines.append(
            f"| `{result['scenario_id']}` | {result['repetition']} | "
            f"`{result['scope']}` | {'PASS' if result['passed'] else 'FAIL'} | "
            f"{result['latency_ms']} | {sources} | {failure} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A failure is a deterministic regression in routing, trusted scope, structured facts, "
            "source citations, SKU direction, context isolation, or safety wording. Latency is "
            "observational and is not a pass/fail threshold. Semantic discovery never counts as "
            "substitution approval.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def run_evaluation(
    *,
    service: RetrievalService,
    classifier: IntentClassifier,
    dataset: Mapping[str, Any],
    mode: str,
    repetitions: int = 3,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
) -> tuple[int, Mapping[str, Any]]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    recording = RecordingService(service)
    as_of = date.fromisoformat(str(dataset["as_of"]))
    results: list[dict[str, Any]] = []
    signatures: dict[str, str] = {}
    for raw_scenario in dataset["scenarios"]:
        scenario = cast(Mapping[str, Any], raw_scenario)
        run_count = repetitions if scenario.get("critical") else 1
        for repetition in range(1, run_count + 1):
            result = _run_scenario(
                scenario,
                repetition=repetition,
                service=recording,
                classifier=classifier,
                as_of=as_of,
            )
            signature = _stability_signature(result)
            previous = signatures.setdefault(str(scenario["id"]), signature)
            if signature != previous:
                result["passed"] = False
                result["failures"].append("deterministic stability signature changed across runs")
            results.append(result)
    failed = sum(not result["passed"] for result in results)
    report: dict[str, Any] = {
        "report_version": 1,
        "dataset_schema_version": dataset["schema_version"],
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repetitions_for_critical_scenarios": repetitions,
        "summary": {
            "total": len(results),
            "passed": len(results) - failed,
            "failed": failed,
        },
        "results": results,
    }
    json_path, markdown_path = _write_reports(report, report_directory)
    print(
        f"golden evaluation mode={mode} passed={len(results) - failed} "
        f"failed={failed} total={len(results)}"
    )
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    return (1 if failed else 0), report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Opt in to read-only Databricks calls")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE_ID)
    parser.add_argument("--vector-index", default=DEFAULT_VECTOR_INDEX)
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    parser.add_argument("--repetitions", type=int, default=3)
    return parser


def main() -> None:
    args = _parser().parse_args()
    dataset = load_golden_dataset(args.dataset)
    if args.live:
        from databricks.sdk import WorkspaceClient

        workspace = WorkspaceClient(
            profile=args.profile,
            product="mediassist-golden-evaluation",
            product_version="0.1.0",
        )
        backend = CachingBackend(DatabricksBackend(workspace, args.warehouse_id))
        service = RetrievalService(backend, vector_index=args.vector_index)
        classifier: IntentClassifier = DatabricksIntentClassifier(
            workspace, args.llm_endpoint
        )
        mode = "live-read-only"
    else:
        service = RetrievalService(
            GoldenFixtureBackend(), vector_index=args.vector_index
        )
        classifier = DeterministicClassifier()
        mode = "local-fixture"
    exit_code, _ = run_evaluation(
        service=service,
        classifier=classifier,
        dataset=dataset,
        mode=mode,
        repetitions=args.repetitions,
        report_directory=args.report_directory,
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
