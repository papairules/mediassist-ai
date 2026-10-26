"""One explicit LangGraph for evidence-grounded MediAssist conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from typing import Any, Mapping, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from crestline_retrieval.models import (
    AuthorizationContext,
    DocumentMatch,
    EvidenceResult,
    EvidenceStatus,
    Outcome,
    SubstitutionVerification,
)
from crestline_retrieval.service import RetrievalService

from .llm import ALLOWED_INTENTS, IntentClassifier
from .tools import (
    AgentTools,
    BackorderEvidenceInput,
    ContractRulesInput,
    DocumentSearchInput,
    OrderVisibilityInput,
    ProductInventoryInput,
    SubstitutionInput,
)


ORDER_PATTERN = re.compile(r"\bORD-\d+\b", re.IGNORECASE)
HOSPITAL_PATTERN = re.compile(r"\bH-[EWC]\d{3}\b", re.IGNORECASE)
SKU_PATTERN = re.compile(r"\b(?:AV|BR|NV)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.IGNORECASE)
LINE_PATTERN = re.compile(r"\bline\s+(\d+)\b", re.IGNORECASE)
REGION_PATTERN = re.compile(r"\b(East|West|Central)\b", re.IGNORECASE)
CANDIDATE_DISCOVERY_PATTERN = re.compile(
    r"\b(?:alternative(?:s)?|replacement(?:s)?|substitute options?|what else)\b",
    re.IGNORECASE,
)
GREETING_ONLY_PATTERN = re.compile(
    r"^\s*(?:hi|hello|hey|good morning|good afternoon|good evening)[\s!,.?]*$",
    re.IGNORECASE,
)
CAPABILITY_PATTERN = re.compile(
    r"\b(?:what can you (?:do|help(?: me)?(?: with)?)|how can you help|what questions can i ask)\b|^\s*help[\s!,.?]*$",
    re.IGNORECASE,
)
REPORTING_PATTERN = re.compile(
    r"\b(?:show|list|summari[sz]e|summary)\b.*\b(?:all orders?|orders?|today|delayed|status)\b"
    r"|\b(?:today['’]?s|daily)\s+(?:order\s+)?(?:status|summary|orders?)\b",
    re.IGNORECASE,
)
UNSUPPORTED_REQUEST_PATTERN = re.compile(
    r"\b(?:create|cancel|delete|update)\s+(?:an?\s+)?(?:order|shipment)|"
    r"\b(?:forecast|dashboard|invoice|billing)\b",
    re.IGNORECASE,
)
DEMO_ORDER_EXAMPLES = {
    "East": ("ORD-1001", "ORD-1002"),
    "West": ("ORD-1003", "ORD-1004"),
    "Central": ("ORD-1006", "ORD-1010"),
}


class AgentState(TypedDict, total=False):
    response: str
    intent: str
    slots: dict[str, str]
    scope_fingerprint: str
    scope_violation: bool
    last_order_id: str
    last_line_id: str
    last_hospital_id: str
    last_sku: str
    last_region: str
    last_requested_sku: str
    last_candidate_sku: str


@dataclass(frozen=True)
class RequestContext:
    service: RetrievalService
    authorization: AuthorizationContext
    classifier: IntentClassifier
    as_of: date
    message: str


@dataclass(frozen=True)
class AgentReply:
    text: str
    intent: str
    thread_id: str


def _scope_fingerprint(auth: AuthorizationContext) -> str:
    scope = {
        "principal_id": auth.principal_id,
        "hospital_regions": sorted(auth.hospital_regions.items()),
        "allowed_regions": sorted(auth.allowed_regions),
        "public_documents": auth.may_search_public_documents,
    }
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _guard_scope(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    current = _scope_fingerprint(runtime.context.authorization)
    saved = state.get("scope_fingerprint")
    if saved is not None and saved != current:
        return {
            "response": (
                "ACCESS_DENIED: this conversation thread belongs to a different trusted "
                "authorization scope. Start a new thread."
            ),
            "intent": "access_denied",
            "scope_violation": True,
            "slots": {},
            "last_order_id": "",
            "last_line_id": "",
            "last_hospital_id": "",
            "last_sku": "",
            "last_region": "",
            "last_requested_sku": "",
            "last_candidate_sku": "",
        }
    return {"scope_fingerprint": current, "scope_violation": False}


def _after_guard(state: AgentState) -> str:
    return "end" if state.get("scope_violation") else "route"


def _extract_slots(message: str) -> dict[str, str]:
    slots: dict[str, str] = {}
    order = ORDER_PATTERN.search(message)
    hospital = HOSPITAL_PATTERN.search(message)
    line = LINE_PATTERN.search(message)
    region = REGION_PATTERN.search(message)
    if order:
        slots["order_id"] = order.group(0).upper()
    if hospital:
        slots["hospital_id"] = hospital.group(0).upper()
    if line:
        slots["line_id"] = line.group(1)
    if region:
        slots["region"] = region.group(1).title()
    skus = [match.group(0).upper() for match in SKU_PATTERN.finditer(message)]
    replace_with = re.search(
        rf"replace\s+({SKU_PATTERN.pattern})\s+with\s+({SKU_PATTERN.pattern})",
        message,
        re.IGNORECASE,
    )
    passive_replace = re.search(
        rf"({SKU_PATTERN.pattern})\s+be\s+replaced\s+with\s+({SKU_PATTERN.pattern})",
        message,
        re.IGNORECASE,
    )
    substitute_with = re.search(
        rf"substitut(?:e|ing)\s+({SKU_PATTERN.pattern})\s+for\s+({SKU_PATTERN.pattern})",
        message,
        re.IGNORECASE,
    )
    candidate_replaces = re.search(
        rf"({SKU_PATTERN.pattern})"
        rf"(?:(?!\b(?:replace|substitute)\b)[^.!?\n]){{0,160}}"
        rf"\b(?:to\s+)?replaces?\s+({SKU_PATTERN.pattern})",
        message,
        re.IGNORECASE,
    )
    candidate_substitutes = re.search(
        rf"({SKU_PATTERN.pattern})\s+substitutes?\s+for\s+({SKU_PATTERN.pattern})",
        message,
        re.IGNORECASE,
    )
    if replace_with:
        slots["requested_sku"] = replace_with.group(1).upper()
        slots["candidate_sku"] = replace_with.group(2).upper()
    elif passive_replace:
        slots["requested_sku"] = passive_replace.group(1).upper()
        slots["candidate_sku"] = passive_replace.group(2).upper()
    elif substitute_with:
        slots["candidate_sku"] = substitute_with.group(1).upper()
        slots["requested_sku"] = substitute_with.group(2).upper()
    elif candidate_replaces:
        slots["candidate_sku"] = candidate_replaces.group(1).upper()
        slots["requested_sku"] = candidate_replaces.group(2).upper()
    elif candidate_substitutes:
        slots["candidate_sku"] = candidate_substitutes.group(1).upper()
        slots["requested_sku"] = candidate_substitutes.group(2).upper()
    elif len(skus) >= 2:
        slots["requested_sku"] = skus[0]
        slots["candidate_sku"] = skus[1]
    elif len(skus) == 1:
        slots["sku"] = skus[0]
    return slots


def _deterministic_intent(message: str, state: AgentState) -> str | None:
    lower = message.lower()
    if GREETING_ONLY_PATTERN.fullmatch(message):
        return "greeting"
    if CAPABILITY_PATTERN.search(message):
        return "capability"
    if REPORTING_PATTERN.search(message):
        return "reporting"
    if UNSUPPORTED_REQUEST_PATTERN.search(message):
        return "unsupported"
    if any(
        word in lower
        for word in (
            "substitute",
            "substitution",
            "replace",
            "replacement",
            "alternative",
            "instead of",
            "what else",
        )
    ):
        return "substitution"
    if any(word in lower for word in ("contract", "brand lock", "brand-lock", "amendment")):
        return "contract"
    if _needs_missing_order_clarification(message, state):
        return "clarification"
    if any(
        word in lower
        for word in ("backorder", "eta", "remaining", "restock", "when will", "arrive", "arrival")
    ):
        if ORDER_PATTERN.search(message) or state.get("last_order_id"):
            return "backorder"
    if any(word in lower for word in ("inventory", "stock", "available", "availability")):
        return "inventory"
    if any(word in lower for word in ("ifu", "guide", "document evidence", "manufacturer instruction")):
        return "document"
    if ORDER_PATTERN.search(message) and any(
        word in lower for word in ("where", "status", "track", "ship", "deliver")
    ):
        return "order"
    return None


def _needs_missing_order_clarification(message: str, state: AgentState) -> bool:
    """Keep context-free quantity follow-ups out of LLM intent routing."""

    lower = message.lower()
    refers_to_units = any(
        word in lower for word in ("remaining", "outstanding", "unshipped")
    )
    has_order_context = bool(ORDER_PATTERN.search(message) or state.get("last_order_id"))
    explicit_inventory = (
        any(word in lower for word in ("inventory", "stock", "available", "availability"))
        and SKU_PATTERN.search(message) is not None
        and REGION_PATTERN.search(message) is not None
    )
    return refers_to_units and not has_order_context and not explicit_inventory


def _safe_classifier_context(state: AgentState) -> dict[str, str]:
    context: dict[str, str] = {}
    for key in (
        "last_order_id",
        "last_line_id",
        "last_hospital_id",
        "last_sku",
        "last_region",
        "last_requested_sku",
        "last_candidate_sku",
    ):
        value = state.get(key)
        if isinstance(value, str) and value:
            context[key] = value
    return context


def _valid_slot(key: str, value: str) -> bool:
    patterns = {
        "order_id": ORDER_PATTERN,
        "hospital_id": HOSPITAL_PATTERN,
        "sku": SKU_PATTERN,
        "requested_sku": SKU_PATTERN,
        "candidate_sku": SKU_PATTERN,
        "line_id": re.compile(r"^\d+$"),
        "region": re.compile(r"^(East|West|Central)$", re.IGNORECASE),
    }
    pattern = patterns.get(key)
    return pattern is not None and pattern.fullmatch(value) is not None


def _route(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    message = runtime.context.message.strip()
    slots = _extract_slots(message)
    intent = _deterministic_intent(message, state)
    if intent is None:
        decision = runtime.context.classifier.classify(message, _safe_classifier_context(state))
        intent = decision.intent if decision.intent in ALLOWED_INTENTS else "clarification"
        for key, value in decision.slots.items():
            if key not in slots and _valid_slot(key, value):
                slots[key] = value.title() if key == "region" else value.upper()
    return {"intent": intent, "slots": slots}


def _route_intent(state: AgentState) -> str:
    return state.get("intent", "clarification")


def _greeting_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    return _complete(
        "Hi! I'm MediAssist AI. I can help you track orders, check supplier restock "
        "estimates, find regional inventory, and review potential product substitutions.\n\n"
        "What would you like to check?"
    )


def _capability_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    return _complete(
        "I can help with order and shipment visibility, supplier restock evidence, "
        "regional inventory, contract and document evidence, and potential substitutions "
        "that always require qualified human clinical review.\n\n"
        "Examples:\n"
        "- Where is ORD-1001?\n"
        "- What is the supplier restock estimate for ORD-1003?\n"
        "- Is BR-IV22-BLU-LF available in East?\n"
        "- Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102?\n\n"
        "Which order, product, or substitution question would you like to check?"
    )


def _reporting_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    examples: list[str] = []
    if runtime.context.authorization.principal_id.startswith("DEMO_ONLY"):
        for region in sorted(runtime.context.authorization.allowed_regions):
            examples.extend(DEMO_ORDER_EXAMPLES.get(region, ()))
    example_text = (
        " For this synthetic scope, example order IDs for individual lookup are: "
        + ", ".join(dict.fromkeys(examples))
        + "."
        if examples
        else " Please provide an order ID that is within your trusted scope."
    )
    return _complete(
        "I understand you want a portfolio or daily order summary. This prototype "
        "currently performs one authorized order lookup at a time; it does not define "
        "or produce a complete delayed-order list or daily status report."
        + example_text
        + " Which order would you like me to inspect?"
    )


def _unsupported_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    return _complete(
        "I understand you are asking for an operational report or action that is not "
        "available in this prototype. I can instead inspect one authorized order, "
        "supplier restock evidence, regional inventory, or a potential substitution. "
        "Which order ID or product question should I check?"
    )


def _citation(row: Mapping[str, Any], record_fields: tuple[str, ...]) -> str:
    record_id = next((row.get(field) for field in record_fields if row.get(field)), "unknown")
    document_id = row.get("document_id", "unknown-document")
    version = row.get("document_version", "unknown-version")
    status = row.get("document_status", "unknown-status")
    page = row.get("page_number", "unknown-page")
    content_hash = row.get("content_sha256", "unknown-hash")
    return (
        f"[{document_id} version={version} status={status} p.{page}; record={record_id}; "
        f"sha256={content_hash}]"
    )


def _access_or_missing(result: EvidenceResult) -> str | None:
    if result.status == EvidenceStatus.ACCESS_DENIED:
        return "ACCESS_DENIED: the requested evidence is outside your trusted authorization scope."
    if result.status == EvidenceStatus.NOT_FOUND:
        return "INSUFFICIENT_EVIDENCE: no authoritative record was found."
    return None


def _order_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    slots = state.get("slots", {})
    order_id = slots.get("order_id") or state.get("last_order_id")
    if not order_id:
        return _clarify("Please provide an order ID such as ORD-1001.")
    result = AgentTools(runtime.context.service, runtime.context.authorization).get_order_visibility(
        OrderVisibilityInput(order_id)
    )
    failure = _access_or_missing(result)
    if failure:
        return _complete(failure, order_id=order_id)
    lines = []
    for row in result.records:
        shipment_citations = []
        for shipment in row.get("shipment_evidence") or []:
            if isinstance(shipment, dict):
                shipment_citations.append(
                    f"[shipment={shipment.get('shipment_id')}; record={shipment.get('record_id')}; "
                    f"tracking={shipment.get('tracking_id')}]"
                )
        lines.append(
            f"{row.get('order_id')} line {row.get('line_id')} ({row.get('sku')}): "
            f"{row.get('quantity_ordered')} ordered, {row.get('quantity_shipped')} shipped, "
            f"{row.get('quantity_carrier_reported_delivered')} carrier-reported delivered, "
            f"{row.get('quantity_unshipped')} unshipped; state={row.get('line_fulfillment_state')}. "
            f"{' '.join(shipment_citations)} [order_record={row.get('order_record_id')}; "
            f"line_record={row.get('order_line_record_id')}]"
        )
    first = result.records[0]
    return _complete(
        "\n".join(lines),
        order_id=order_id,
        line_id=str(first.get("line_id") or ""),
        hospital_id=str(first.get("hospital_id") or ""),
        sku=str(first.get("sku") or ""),
        region=str(first.get("region") or ""),
    )


def _backorder_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    slots = state.get("slots", {})
    order_id = slots.get("order_id") or state.get("last_order_id")
    line_id = slots.get("line_id") or state.get("last_line_id") or None
    if not order_id:
        return _clarify("Which order should I check for backorder or supplier ETA evidence?")
    result = AgentTools(runtime.context.service, runtime.context.authorization).get_backorder_evidence(
        BackorderEvidenceInput(order_id, line_id)
    )
    failure = _access_or_missing(result)
    if failure:
        return _complete(failure, order_id=order_id)
    rendered = []
    for row in result.records:
        eta = row.get("current_supplier_restock_date")
        eta_text = (
            f"latest supplier restock estimate={eta}; this is not a hospital delivery promise"
            if eta is not None
            else "supplier restock date unknown; hospital delivery date unknown"
        )
        rendered.append(
            f"{row.get('order_id')} line {row.get('line_id')}: "
            f"{row.get('quantity_unshipped')} units unshipped; {eta_text}. "
            f"[supplier_record={row.get('current_supplier_eta_record_id')}; "
            f"bronze_record={row.get('current_supplier_eta_bronze_record_id')}; "
            f"source_file={row.get('current_supplier_eta_source_file_name')}]"
        )
    first = result.records[0]
    return _complete(
        "\n".join(rendered),
        order_id=order_id,
        line_id=str(first.get("line_id") or ""),
        hospital_id=str(first.get("hospital_id") or ""),
        sku=str(first.get("sku") or ""),
        region=str(first.get("region") or ""),
    )


def _inventory_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    slots = state.get("slots", {})
    sku = slots.get("sku") or slots.get("candidate_sku") or state.get("last_sku")
    region = slots.get("region") or state.get("last_region")
    if not sku or not region:
        return _clarify("Please provide both a product SKU and region for inventory availability.")
    result = AgentTools(runtime.context.service, runtime.context.authorization).get_product_inventory(
        ProductInventoryInput(region, sku)
    )
    failure = _access_or_missing(result)
    if failure:
        return _complete(failure, sku=sku, region=region)
    row = result.records[0]
    observed = str(row.get("has_inventory_observation")).lower() == "true"
    if not observed:
        text = f"{sku} in {region}: no current inventory observation; availability is unknown."
    else:
        text = (
            f"{sku} in {region}: observed available quantity={row.get('quantity_available')} "
            f"as of {row.get('latest_current_snapshot_at')}."
        )
    evidence = []
    for item in row.get("warehouse_inventory_evidence") or []:
        if isinstance(item, dict):
            evidence.append(
                f"[warehouse={item.get('warehouse_id')}; snapshot={item.get('snapshot_at')}; "
                f"record={item.get('record_id')}]"
            )
    return _complete(
        f"{text} {' '.join(evidence)} [product_record={row.get('product_record_id')}]",
        sku=sku,
        region=region,
    )


def _contract_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    hospital_id = state.get("slots", {}).get("hospital_id")
    if not hospital_id:
        return _clarify("Which hospital's contract rules should I check?")
    result = AgentTools(runtime.context.service, runtime.context.authorization).get_contract_rules(
        ContractRulesInput(hospital_id, runtime.context.as_of)
    )
    failure = _access_or_missing(result)
    if failure:
        return _complete(failure)
    message = runtime.context.message.lower()
    if "pricing" in message or "price" in message:
        prefix = "Confidential pricing is excluded from this agent. Authorized non-pricing rules:"
    else:
        prefix = f"Authorized non-pricing contract rules for {hospital_id}:"
    rules = [prefix]
    for row in result.records:
        rules.append(
            f"- {row.get('clause_type')}: {row.get('clause_text')} "
            f"{_citation(row, ('contract_clause_id',))}"
        )
    return _complete("\n".join(rules))


def _document_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    message = runtime.context.message
    result = AgentTools(runtime.context.service, runtime.context.authorization).search_document_evidence(
        DocumentSearchInput(message)
    )
    if result.status == EvidenceStatus.ACCESS_DENIED:
        return _complete("ACCESS_DENIED: public document discovery is not authorized.")
    if result.status == EvidenceStatus.NOT_FOUND:
        return _complete("INSUFFICIENT_EVIDENCE: no matching document evidence was found.")
    lines = ["Semantic discovery only; these similarity matches are not approval evidence:"]
    for match in result.matches:
        lines.append(f"- {match.embedding_text} {_match_citation(match)}")
    return _complete("\n".join(lines))


def _match_citation(match: DocumentMatch) -> str:
    return (
        f"[{match.document_id} version={match.document_version} "
        f"status={match.document_status} p.{match.page_number}; "
        f"record={match.source_record_id}; sha256={match.content_sha256}; "
        f"discovery_score={match.discovery_score}]"
    )


def _substitution_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    slots = dict(state.get("slots", {}))
    explicit_order_id = slots.get("order_id")
    explicit_hospital_id = slots.get("hospital_id")
    inherited_hospital_id = state.get("last_hospital_id") or None
    incompatible_inherited_order = (
        explicit_order_id is None
        and explicit_hospital_id is not None
        and inherited_hospital_id is not None
        and explicit_hospital_id != inherited_hospital_id
    )
    order_id = explicit_order_id or (
        None if incompatible_inherited_order else state.get("last_order_id") or None
    )
    line_id = slots.get("line_id") or (
        None if incompatible_inherited_order else state.get("last_line_id") or None
    )
    requested = (
        slots.get("requested_sku")
        or (None if incompatible_inherited_order else state.get("last_requested_sku"))
        or (None if incompatible_inherited_order else state.get("last_sku"))
        or None
    )
    discovery_requested = (
        slots.get("candidate_sku") is None
        and slots.get("sku") is None
        and CANDIDATE_DISCOVERY_PATTERN.search(runtime.context.message) is not None
    )
    candidate = slots.get("candidate_sku") or slots.get("sku")
    if candidate is None and not discovery_requested:
        candidate = state.get("last_candidate_sku") or None
    hospital_id = explicit_hospital_id
    region = slots.get("region") or (
        None if incompatible_inherited_order else state.get("last_region") or None
    )
    tools = AgentTools(runtime.context.service, runtime.context.authorization)

    if order_id:
        order = tools.get_order_visibility(OrderVisibilityInput(order_id))
        failure = _access_or_missing(order)
        if failure:
            return _complete(failure, order_id=order_id)
        matching = [
            row
            for row in order.records
            if line_id is None or str(row.get("line_id")) == line_id
        ]
        if len(matching) != 1:
            return _clarify("Please specify the order line for this substitution request.")
        row = matching[0]
        derived_hospital = str(row.get("hospital_id") or "")
        derived_region = str(row.get("region") or "")
        derived_requested = str(row.get("sku") or "")
        if not derived_hospital or not derived_region or not derived_requested:
            return _complete(
                "INSUFFICIENT_EVIDENCE: authoritative order evidence is missing the hospital, "
                "region, or requested SKU.",
                order_id=order_id,
                line_id=line_id or "",
            )
        if hospital_id and hospital_id != derived_hospital:
            return _complete("ACCESS_DENIED: the supplied hospital conflicts with authorized order evidence.")
        if region and region != derived_region:
            return _complete("ACCESS_DENIED: the supplied region conflicts with authorized order evidence.")
        if requested and requested != derived_requested:
            return _complete(
                "INSUFFICIENT_EVIDENCE: the requested SKU conflicts with authoritative order evidence.",
                order_id=order_id,
                line_id=str(row.get("line_id") or ""),
            )
        requested = derived_requested
        hospital_id = derived_hospital
        region = derived_region
        line_id = str(row.get("line_id") or "")

    if discovery_requested and order_id is None:
        result = _clarify("Which authorized order should I use to discover substitution candidates?")
        return _clear_order_context(result) if incompatible_inherited_order else result
    if hospital_id and region is None:
        region = runtime.context.authorization.hospital_region(hospital_id)
    if requested is None or hospital_id is None or region is None:
        result = _clarify(
            "Please provide the missing order context, requested SKU, hospital, or region."
        )
        return _clear_order_context(result) if incompatible_inherited_order else result
    if discovery_requested:
        return _discover_and_verify_candidates(
            tools,
            requested_sku=requested,
            hospital_id=hospital_id,
            region=region,
            as_of=runtime.context.as_of,
            order_id=order_id,
            line_id=line_id,
        )
    if candidate is None:
        result = _clarify("Which candidate SKU should I verify, or should I discover alternatives?")
        return _clear_order_context(result) if incompatible_inherited_order else result
    verification = tools.verify_substitution(
        SubstitutionInput(
            requested_sku=requested,
            candidate_sku=candidate,
            hospital_id=hospital_id,
            region=region,
            as_of=runtime.context.as_of,
            order_id=order_id,
            line_id=line_id,
            request_text=runtime.context.message,
        )
    )
    text = _render_verification(verification)
    return _complete(
        text,
        order_id=order_id or "",
        line_id=line_id or "",
        hospital_id=hospital_id,
        sku=candidate,
        region=region,
        requested_sku=requested,
        candidate_sku=candidate,
        clear_order_context=incompatible_inherited_order,
    )


def _effective_on(match: DocumentMatch, as_of: date) -> bool:
    try:
        return date.fromisoformat(match.effective_from[:10]) <= as_of
    except ValueError:
        return False


def _discover_and_verify_candidates(
    tools: AgentTools,
    *,
    requested_sku: str,
    hospital_id: str,
    region: str,
    as_of: date,
    order_id: str | None,
    line_id: str | None,
) -> AgentState:
    discovery = tools.search_document_evidence(
        DocumentSearchInput(
            query=f"current substitution guide candidates for requested SKU {requested_sku}",
            num_results=20,
        )
    )
    if discovery.status == EvidenceStatus.ACCESS_DENIED:
        return _complete("ACCESS_DENIED: public substitution-guide discovery is not authorized.")
    if discovery.status != EvidenceStatus.FOUND:
        return _complete(
            "INSUFFICIENT_EVIDENCE: no current substitution-guide candidates were discovered.",
            order_id=order_id or "",
            line_id=line_id or "",
            sku=requested_sku,
            region=region,
            requested_sku=requested_sku,
        )

    current_matches: dict[str, DocumentMatch] = {}
    ordered_matches = sorted(
        discovery.matches,
        key=lambda match: (
            match.candidate_sku or "",
            match.effective_from,
            match.document_id,
            match.source_record_id,
        ),
    )
    for match in ordered_matches:
        candidate_sku = match.candidate_sku
        if (
            match.chunk_type != "substitution_guide_row"
            or match.requested_sku != requested_sku
            or candidate_sku is None
            or candidate_sku == requested_sku
            or match.document_status.lower() != "current"
            or not _effective_on(match, as_of)
        ):
            continue
        current_matches.setdefault(candidate_sku, match)
    if not current_matches:
        return _complete(
            "INSUFFICIENT_EVIDENCE: semantic discovery returned no current, effective guide "
            "rows for the requested SKU.",
            order_id=order_id or "",
            line_id=line_id or "",
            sku=requested_sku,
            region=region,
            requested_sku=requested_sku,
        )

    verified: list[tuple[DocumentMatch, SubstitutionVerification]] = []
    for candidate_sku, match in sorted(current_matches.items()):
        result = tools.verify_substitution(
            SubstitutionInput(
                requested_sku=requested_sku,
                candidate_sku=candidate_sku,
                hospital_id=hospital_id,
                region=region,
                as_of=as_of,
                order_id=order_id,
                line_id=line_id,
            )
        )
        if result.outcome == Outcome.ACCESS_DENIED:
            return _complete(
                "ACCESS_DENIED: candidate verification fell outside the trusted authorization scope."
            )
        verified.append((match, result))

    lines = [
        (
            "Candidate discovery used semantic guide search only to identify exact SKU pairs. "
            "Every outcome below comes from deterministic verification and is not clinical approval."
        ),
        f"Verified alternatives for {requested_sku} on {order_id} line {line_id}:",
    ]
    for match, result in verified:
        lines.append(f"\nCandidate {result.candidate_sku}:")
        lines.append(_render_verification(result))
        lines.append(f"Discovery source only—not approval evidence: {_match_citation(match)}")
    return _complete(
        "\n".join(lines),
        order_id=order_id or "",
        line_id=line_id or "",
        hospital_id=hospital_id,
        sku=requested_sku,
        region=region,
        requested_sku=requested_sku,
    )


def _render_verification(value: SubstitutionVerification) -> str:
    if value.outcome == Outcome.ELIGIBLE_FOR_HUMAN_REVIEW:
        lead = (
            "ELIGIBLE_FOR_HUMAN_REVIEW—not approved. All deterministic preconditions "
            "passed, but qualified human clinical review is still required."
        )
    elif value.outcome == Outcome.BLOCKED:
        lead = "BLOCKED. The candidate must not be presented as an eligible substitution."
    elif value.outcome == Outcome.ACCESS_DENIED:
        lead = "ACCESS_DENIED. No protected evidence is available in this authorization scope."
    else:
        lead = "INSUFFICIENT_EVIDENCE. Do not infer a substitution or clinical exception."
    lines = [
        lead,
        (
            f"Evaluated pair: requested={value.requested_sku}; "
            f"candidate={value.candidate_sku}."
        ),
        f"Reasons: {', '.join(value.reason_codes) or 'none supplied'}.",
    ]
    for row in value.guide_evidence:
        lines.append(f"Guide: status={row.get('row_status')} {_citation(row, ('substitution_row_id',))}")
    for row in value.ifu_evidence:
        lines.append(f"IFU: product={row.get('product_sku')} {_citation(row, ('ifu_fact_id',))}")
    for row in value.contract_evidence:
        exact_override = (
            str(row.get("is_override", "")).lower() == "true"
            and row.get("requested_sku") == value.requested_sku
            and row.get("candidate_sku") == value.candidate_sku
        )
        conditions_verified = value.outcome == Outcome.ELIGIBLE_FOR_HUMAN_REVIEW
        label = (
            "Applicable amendment verified"
            if exact_override and conditions_verified
            else "Contract evidence reviewed"
        )
        lines.append(
            f"{label}: clause={row.get('clause_type')} "
            f"{_citation(row, ('contract_clause_id',))}"
        )
    for row in value.inventory_evidence:
        observed = row.get("has_inventory_observation")
        lines.append(
            f"Inventory: observed={observed}, available={row.get('quantity_available')}; "
            f"product_record={row.get('product_record_id')}."
        )
    for row in value.backorder_evidence:
        lines.append(
            f"Backorder: order={row.get('order_id')} line={row.get('line_id')} "
            f"unshipped={row.get('quantity_unshipped')}; "
            f"supplier_record={row.get('current_supplier_eta_record_id')}."
        )
    return "\n".join(lines)


def _clarification_node(state: AgentState, runtime: Runtime[RequestContext]) -> AgentState:
    if _needs_missing_order_clarification(runtime.context.message, state):
        return _clarify("Which order should I check for the remaining or unshipped units?")
    lower = runtime.context.message.lower()
    if any(word in lower for word in ("eta", "restock", "arrive", "arrival", "backorder")):
        return _clarify("Which order ID should I check for supplier ETA evidence?")
    if any(word in lower for word in ("inventory", "stock", "available", "availability")):
        return _clarify("Which product SKU and region should I check for inventory?")
    if any(word in lower for word in ("substitute", "substitution", "replace", "alternative")):
        return _clarify(
            "Please provide the requested SKU, candidate SKU, hospital, and order line "
            "if the request is conditional."
        )
    if any(word in lower for word in ("contract", "brand lock", "amendment")):
        return _clarify("Which hospital's contract or amendment should I check?")
    if any(word in lower for word in ("order", "shipment", "track", "deliver", "status")):
        return _clarify("Which order ID should I check?")
    return _clarify(
        "I can help with an order ID, supplier ETA, product SKU and region, or an "
        "exact substitution question. What would you like to check?"
    )


def _clarify(message: str) -> AgentState:
    return _complete(f"CLARIFICATION_REQUIRED: {message}")


def _clear_order_context(update: AgentState) -> AgentState:
    update["last_order_id"] = ""
    update["last_line_id"] = ""
    return update


def _complete(
    response: str,
    *,
    order_id: str = "",
    line_id: str = "",
    hospital_id: str = "",
    sku: str = "",
    region: str = "",
    requested_sku: str = "",
    candidate_sku: str = "",
    clear_order_context: bool = False,
) -> AgentState:
    update: AgentState = {"response": response, "slots": {}}
    if clear_order_context:
        _clear_order_context(update)
    if order_id:
        update["last_order_id"] = order_id
    if line_id:
        update["last_line_id"] = line_id
    if hospital_id:
        update["last_hospital_id"] = hospital_id
    if sku:
        update["last_sku"] = sku
    if region:
        update["last_region"] = region
    if requested_sku:
        update["last_requested_sku"] = requested_sku
    if candidate_sku:
        update["last_candidate_sku"] = candidate_sku
    return update


def build_graph(checkpointer: Any | None = None) -> Any:
    graph = StateGraph(AgentState, context_schema=RequestContext)
    graph.add_node("guard", _guard_scope)
    graph.add_node("route", _route)
    graph.add_node("greeting", _greeting_node)
    graph.add_node("capability", _capability_node)
    graph.add_node("reporting", _reporting_node)
    graph.add_node("unsupported", _unsupported_node)
    graph.add_node("order", _order_node)
    graph.add_node("backorder", _backorder_node)
    graph.add_node("inventory", _inventory_node)
    graph.add_node("contract", _contract_node)
    graph.add_node("substitution", _substitution_node)
    graph.add_node("document", _document_node)
    graph.add_node("clarification", _clarification_node)
    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", _after_guard, {"route": "route", "end": END})
    graph.add_conditional_edges(
        "route",
        _route_intent,
        {
            "greeting": "greeting",
            "capability": "capability",
            "reporting": "reporting",
            "unsupported": "unsupported",
            "order": "order",
            "backorder": "backorder",
            "inventory": "inventory",
            "contract": "contract",
            "substitution": "substitution",
            "document": "document",
            "clarification": "clarification",
        },
    )
    for node in (
        "order",
        "greeting",
        "capability",
        "reporting",
        "unsupported",
        "backorder",
        "inventory",
        "contract",
        "substitution",
        "document",
        "clarification",
    ):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


class MediAssistAgent:
    def __init__(self, checkpointer: Any | None = None) -> None:
        self._graph = build_graph(checkpointer)

    def invoke(
        self,
        message: str,
        *,
        thread_id: str,
        service: RetrievalService,
        authorization: AuthorizationContext,
        classifier: IntentClassifier,
        as_of: date,
    ) -> AgentReply:
        if not thread_id.strip():
            raise ValueError("thread_id is required for checkpoint isolation")
        result = self._graph.invoke(
            {},
            {"configurable": {"thread_id": thread_id}},
            context=RequestContext(service, authorization, classifier, as_of, message),
        )
        return AgentReply(
            text=str(result.get("response", "CLARIFICATION_REQUIRED: No response generated.")),
            intent=str(result.get("intent", "clarification")),
            thread_id=thread_id,
        )
