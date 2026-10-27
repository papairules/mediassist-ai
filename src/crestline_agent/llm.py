"""Narrow LLM boundary used only for intent and slot classification."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Mapping, Protocol


LOGGER = logging.getLogger(__name__)


ALLOWED_INTENTS = frozenset(
    {
        "order",
        "backorder",
        "inventory",
        "contract",
        "substitution",
        "document",
        "clarification",
        "resolution",
    }
)


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    slots: Mapping[str, str] = field(default_factory=dict)


class IntentClassifier(Protocol):
    def classify(self, message: str, safe_context: Mapping[str, str]) -> IntentDecision: ...


class DatabricksIntentClassifier:
    """Query a configured existing endpoint; never use model text as evidence."""

    def __init__(self, workspace_client: Any, endpoint_name: str) -> None:
        if not endpoint_name.strip():
            raise ValueError("A Databricks LLM endpoint name is required")
        self._workspace = workspace_client
        self._endpoint_name = endpoint_name

    def classify(self, message: str, safe_context: Mapping[str, str]) -> IntentDecision:
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        prompt = (
            "Classify a medical-distribution support request. Return JSON only with keys "
            '"intent" and "slots". intent must be one of order, backorder, inventory, '
            "contract, substitution, document, resolution, clarification. slots may contain only order_id, "
            "line_id, sku, requested_sku, candidate_sku, hospital_id, region. Do not "
            "answer the question, infer authorization, or make a clinical decision.\n"
            f"Safe prior references: {json.dumps(dict(safe_context), sort_keys=True)}\n"
            f"Request: {message}"
        )
        started = time.monotonic()
        LOGGER.info("qwen_request_started")
        try:
            response = self._workspace.serving_endpoints.query(
                name=self._endpoint_name,
                messages=[
                    ChatMessage(role=ChatMessageRole.SYSTEM, content="Return strict JSON only."),
                    ChatMessage(role=ChatMessageRole.USER, content=prompt),
                ],
                temperature=0.0,
                max_tokens=200,
            )
        except Exception:
            LOGGER.exception(
                "qwen_request_failed elapsed_ms=%d",
                int((time.monotonic() - started) * 1000),
            )
            raise
        LOGGER.info(
            "qwen_response_received elapsed_ms=%d",
            int((time.monotonic() - started) * 1000),
        )
        choices = getattr(response, "choices", None)
        if not choices:
            return IntentDecision("clarification")
        choice = choices[0]
        model_message = getattr(choice, "message", None)
        content = getattr(model_message, "content", None) or getattr(choice, "text", None)
        if not isinstance(content, str):
            return IntentDecision("clarification")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return IntentDecision("clarification")
        if not isinstance(payload, dict) or payload.get("intent") not in ALLOWED_INTENTS:
            return IntentDecision("clarification")
        raw_slots = payload.get("slots", {})
        if not isinstance(raw_slots, dict):
            raw_slots = {}
        slots = {
            str(key): str(value)
            for key, value in raw_slots.items()
            if key
            in {
                "order_id",
                "line_id",
                "sku",
                "requested_sku",
                "candidate_sku",
                "hospital_id",
                "region",
            }
            and isinstance(value, str)
        }
        return IntentDecision(str(payload["intent"]), slots)
