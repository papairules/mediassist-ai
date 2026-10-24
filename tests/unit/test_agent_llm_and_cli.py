from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from crestline_agent.cli import build_demo_only_authorization
from crestline_agent.llm import DatabricksIntentClassifier


class FakeServingEndpoints:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content), text=None)]
        )


class FakeWorkspace:
    def __init__(self, content: str) -> None:
        self.serving_endpoints = FakeServingEndpoints(content)


def test_model_endpoint_is_configured_and_limited_to_intent_json() -> None:
    workspace = FakeWorkspace(
        '{"intent":"inventory","slots":{"sku":"BR-IV22-BLU-LF","region":"East"}}'
    )
    classifier = DatabricksIntentClassifier(workspace, "existing-chat-endpoint")
    decision = classifier.classify("Do we have stock?", {"last_order_id": "ORD-1001"})
    assert decision.intent == "inventory"
    assert decision.slots == {"sku": "BR-IV22-BLU-LF", "region": "East"}
    call = workspace.serving_endpoints.calls[0]
    assert call["name"] == "existing-chat-endpoint"
    assert call["temperature"] == 0.0
    assert "infer authorization" in call["messages"][1].content


@pytest.mark.parametrize("content", ["not json", '{"intent":"approve_substitute"}'])
def test_invalid_model_output_fails_to_clarification(content: str) -> None:
    classifier = DatabricksIntentClassifier(FakeWorkspace(content), "existing-chat-endpoint")
    assert classifier.classify("ambiguous", {}).intent == "clarification"


def test_model_endpoint_name_is_required() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        DatabricksIntentClassifier(FakeWorkspace("{}"), "")


def test_cli_demo_authorization_is_explicit_and_rebuilt_per_request() -> None:
    first = build_demo_only_authorization("east")
    second = build_demo_only_authorization("east")
    west = build_demo_only_authorization("west")
    assert first is not second
    assert first.principal_id == "DEMO_ONLY_cli_east"
    assert set(first.hospital_regions) == {"H-E101", "H-E102"}
    assert set(west.hospital_regions) == {"H-W201", "H-W202"}
    assert first.hospital_regions != west.hospital_regions
