"""Pure presentation parsing for already-validated agent responses."""

from __future__ import annotations

from dataclasses import dataclass
import re


OUTCOMES = (
    "ELIGIBLE_FOR_HUMAN_REVIEW",
    "INSUFFICIENT_EVIDENCE",
    "ACCESS_DENIED",
    "BLOCKED",
)
CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")
DOCUMENT_CITATION_PATTERN = re.compile(
    r"^(?P<document_id>\S+)\s+version=(?P<version>\S+)"
    r"(?:\s+status=(?P<status>\S+))?\s+p\.(?P<page>[^;]+);\s*"
    r"record=(?P<record_id>[^;]+);\s*sha256=(?P<sha256>[^;\]]+)"
    r"(?:;\s*discovery_score=(?P<score>[^;\]]+))?$"
)
CANDIDATE_OUTCOME_PATTERN = re.compile(
    r"Candidate\s+(?P<sku>[A-Z]{2}-[A-Z0-9-]+):\s*\n"
    r"(?P<outcome>ELIGIBLE_FOR_HUMAN_REVIEW|INSUFFICIENT_EVIDENCE|BLOCKED)"
)
REASON_LABELS = {
    "current_guide_status_blocks_candidate": "Current guide status blocks this candidate.",
    "current_approved_guide_row_missing_or_conflicting": (
        "No single current approved guide row supports this pair."
    ),
    "complete_current_ifu_pair_missing_or_conflicting": "Complete current IFU evidence is missing.",
    "ifu_attributes_do_not_match": "Required IFU product attributes do not match.",
    "invalid_ifu_interchangeability_claim": "IFU evidence does not establish interchangeability.",
    "effective_contract_evidence_missing": "Effective contract evidence is missing.",
    "contract_brand_restriction_without_exact_override": (
        "A brand restriction has no applicable exact override."
    ),
    "conditional_amendment_requires_order_line_context": (
        "The conditional amendment requires order-line context."
    ),
    "conditional_amendment_backorder_evidence_missing": "Required backorder evidence is missing.",
    "conditional_amendment_has_no_open_backorder": "The amendment has no qualifying open backorder.",
    "regional_inventory_evidence_missing_or_conflicting": "Regional inventory evidence is incomplete.",
    "regional_inventory_observation_missing": "No current regional inventory observation exists.",
    "candidate_has_no_observed_available_inventory": "No observed available inventory exists.",
    "all_deterministic_preconditions_met_human_clinical_review_required": (
        "Deterministic checks passed; qualified clinical review is still required."
    ),
}


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    detail: str = ""


@dataclass(frozen=True)
class Citation:
    document_id: str | None
    version: str | None
    status: str | None
    page: str | None
    record_id: str | None
    sha256: str | None
    details: tuple[tuple[str, str], ...] = ()
    row_status: str | None = None

    @property
    def is_historical(self) -> bool:
        return (self.status or "").lower() in {"superseded", "historical", "revoked"} or (
            self.row_status or ""
        ).upper() in {"REVOKED", "APPROVED (REVOKED)"}

    @property
    def is_current(self) -> bool:
        return (self.status or "").lower() == "current" and not self.is_historical


@dataclass(frozen=True)
class CandidateOutcome:
    sku: str
    outcome: str
    reason_codes: tuple[str, ...]
    human_reasons: tuple[str, ...]
    inventory_available: str | None
    inventory_observed: bool | None
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class ResponseView:
    text: str
    display_text: str
    outcome: str | None
    metrics: tuple[Metric, ...]
    citations: tuple[Citation, ...]
    candidate_outcomes: tuple[CandidateOutcome, ...] = ()
    summary_text: str = ""
    brief_explanation: str = ""
    supporting_text: str = ""

    @property
    def candidate_summary(self) -> str | None:
        if not self.candidate_outcomes:
            return None
        if all(candidate.outcome == "BLOCKED" for candidate in self.candidate_outcomes):
            return "No eligible alternatives currently identified"
        eligible = sum(
            candidate.outcome == "ELIGIBLE_FOR_HUMAN_REVIEW"
            for candidate in self.candidate_outcomes
        )
        blocked = sum(candidate.outcome == "BLOCKED" for candidate in self.candidate_outcomes)
        insufficient = sum(
            candidate.outcome == "INSUFFICIENT_EVIDENCE"
            for candidate in self.candidate_outcomes
        )
        review_text = (
            "1 requires clinical review"
            if eligible == 1
            else f"{eligible} require clinical review"
        )
        insufficient_text = (
            "1 has insufficient evidence"
            if insufficient == 1
            else f"{insufficient} have insufficient evidence"
        )
        return (
            f"{len(self.candidate_outcomes)} alternatives evaluated: "
            f"{review_text}, {blocked} blocked, {insufficient_text}"
        )

    @property
    def candidate_headline(self) -> str | None:
        """Concise decision headline for candidate discovery."""
        if not self.candidate_outcomes:
            return None
        outcomes = {candidate.outcome for candidate in self.candidate_outcomes}
        if outcomes == {"BLOCKED"}:
            return "No — no eligible alternatives currently identified."
        if "ELIGIBLE_FOR_HUMAN_REVIEW" in outcomes:
            return "Conditionally eligible — human review required."
        return "Insufficient evidence — no candidate is currently eligible."


def _key_values(raw: str) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for part in raw.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key and value:
            values.append((key.strip(), value.strip()))
    return tuple(values)


def _citation(raw: str, context: str = "") -> Citation:
    guide_status = re.search(r"Guide:\s*status=(?P<status>.+?)\s*$", context)
    row_status = guide_status.group("status").strip() if guide_status else None
    document = DOCUMENT_CITATION_PATTERN.match(raw.strip())
    if document:
        values = document.groupdict()
        score_details: tuple[tuple[str, str], ...] = ()
        if values.get("score"):
            score_details = (("discovery_score", str(values["score"])),)
        return Citation(
            document_id=values["document_id"],
            version=values["version"],
            status=values.get("status"),
            page=values["page"].strip(),
            record_id=values["record_id"].strip(),
            sha256=values["sha256"].strip(),
            details=score_details,
            row_status=row_status,
        )
    pairs = _key_values(raw)
    record_id = next(
        (value for key, value in pairs if key == "record" or key.endswith("_record")),
        None,
    )
    return Citation(
        document_id=None,
        version=None,
        status=None,
        page=None,
        record_id=record_id,
        sha256=None,
        details=pairs,
    )


def _citations(text: str) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    for match in CITATION_PATTERN.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        citations.append(_citation(match.group(1), text[line_start : match.start()]))
    return tuple(citations)


def _reason_codes(section: str) -> tuple[str, ...]:
    match = re.search(r"^Reasons:\s*(?P<reasons>[^.\n]+)", section, re.MULTILINE)
    if not match or match.group("reasons").strip() == "none supplied":
        return ()
    return tuple(reason.strip() for reason in match.group("reasons").split(",") if reason.strip())


def _human_reasons(reason_codes: tuple[str, ...]) -> tuple[str, ...]:
    labels = tuple(
        REASON_LABELS.get(code, code.replace("_", " ").capitalize() + ".")
        for code in reason_codes
    )
    if len(labels) <= 3:
        return labels
    return labels[:3] + (f"{len(labels) - 3} additional checks require attention.",)


def _operational_citations(section: str) -> tuple[Citation, ...]:
    details: list[tuple[str, str]] = []
    for field in ("order_record", "order_line_record", "product_record", "supplier_record"):
        for value in re.findall(rf"\b{field}=([^.;\s]+)", section):
            pair = (field, value)
            if pair not in details:
                details.append(pair)
    if not details:
        return ()
    return (
        Citation(
            document_id=None,
            version=None,
            status=None,
            page=None,
            record_id=details[0][1],
            sha256=None,
            details=tuple(details),
        ),
    )


def _candidate_outcomes(text: str) -> tuple[CandidateOutcome, ...]:
    matches = list(CANDIDATE_OUTCOME_PATTERN.finditer(text))
    outcomes: list[CandidateOutcome] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end]
        reason_codes = _reason_codes(section)
        inventory = re.search(
            r"Inventory:\s*observed=(?P<observed>[^,;]+),\s*available=(?P<available>[^;\n]+)",
            section,
        )
        inventory_observed: bool | None = None
        inventory_available: str | None = None
        if inventory:
            inventory_observed = inventory.group("observed").strip().lower() == "true"
            inventory_available = inventory.group("available").strip()
        outcomes.append(
            CandidateOutcome(
                sku=match.group("sku"),
                outcome=match.group("outcome"),
                reason_codes=reason_codes,
                human_reasons=_human_reasons(reason_codes),
                inventory_available=inventory_available,
                inventory_observed=inventory_observed,
                citations=_citations(section) + _operational_citations(section),
            )
        )
    return tuple(outcomes)


def _metrics(text: str, *, include_substitution_inventory: bool = True) -> tuple[Metric, ...]:
    metrics: list[Metric] = []
    order = re.search(
        r"(?P<ordered>\d+) ordered, (?P<shipped>\d+) shipped, "
        r"(?P<delivered>\d+) carrier-reported delivered, (?P<unshipped>\d+) unshipped; "
        r"state=(?P<state>[\w-]+)",
        text,
    )
    if order:
        metrics.extend(
            (
                Metric("Ordered", order.group("ordered"), "units"),
                Metric("Shipped", order.group("shipped"), "units"),
                Metric("Unshipped", order.group("unshipped"), "units"),
                Metric("Fulfillment", order.group("state").replace("_", " ").title()),
            )
        )
    backorder = re.search(r"(?P<count>\d+) units unshipped", text)
    if backorder and not order:
        metrics.append(Metric("Unshipped", backorder.group("count"), "units"))
    supplier_date = re.search(r"latest supplier restock estimate=(?P<date>[^;\n]+)", text)
    if supplier_date:
        metrics.append(Metric("Supplier restock estimate", supplier_date.group("date").strip()))
    elif "supplier restock date unknown" in text.lower():
        metrics.append(Metric("Supplier restock estimate", "Unknown", "No date reported"))
    inventory = re.search(r"observed available quantity=(?P<count>-?\d+)", text)
    if inventory:
        metrics.append(Metric("Available inventory", inventory.group("count"), "observed units"))
    substitution_inventory = re.search(r"Inventory: observed=(?P<observed>[^,;]+), available=(?P<count>[^;\n]+)", text)
    if substitution_inventory and include_substitution_inventory:
        observed = substitution_inventory.group("observed").strip().lower() == "true"
        detail = "observed units" if observed else "observation missing"
        metrics.append(Metric("Candidate inventory", substitution_inventory.group("count").strip(), detail))
    return tuple(metrics)


def _brief_substitution(outcome: str, text: str) -> tuple[str, str]:
    pair = re.search(
        r"Evaluated pair:\s*requested=(?P<requested>[^;]+);\s*candidate=(?P<candidate>[^.]+)",
        text,
    )
    pair_text = ""
    if pair:
        pair_text = (
            f"Requested {pair.group('requested').strip()}; "
            f"candidate {pair.group('candidate').strip()}."
        )
    reasons = _human_reasons(_reason_codes(text))
    reason = reasons[0] if reasons else "The deterministic checks did not provide further detail."
    if outcome == "BLOCKED":
        return "No — this substitution is blocked.", " ".join((pair_text, reason)).strip()
    if outcome == "ELIGIBLE_FOR_HUMAN_REVIEW":
        return (
            "Conditionally eligible — human review required.",
            " ".join((pair_text, "Not approved. Qualified clinical review is required.")).strip(),
        )
    if outcome == "ACCESS_DENIED":
        return "Access denied.", "The requested evidence is outside the trusted authorization scope."
    return (
        "Insufficient evidence.",
        " ".join((pair_text, reason, "No substitution approval can be inferred.")).strip(),
    )


def _operational_summary(text: str, metrics: tuple[Metric, ...]) -> tuple[str, str]:
    order = re.search(
        r"(?P<order>ORD-\d+)\s+line\s+(?P<line>\d+).*?state=(?P<state>[\w-]+)",
        text,
        re.IGNORECASE,
    )
    if order:
        ordered = next((metric.value for metric in metrics if metric.label == "Ordered"), None)
        unshipped = next((metric.value for metric in metrics if metric.label == "Unshipped"), None)
        state = order.group("state").replace("_", " ")
        explanation = f"{ordered} units ordered; {unshipped} remain unshipped." if ordered and unshipped else "See the fulfillment metrics for the reported quantities."
        return (
            f"{order.group('order')} line {order.group('line')}: {state.title()}.",
            explanation,
        )
    supplier = re.search(r"latest supplier restock estimate=(?P<date>[^;\n]+)", text)
    if supplier:
        return (
            f"Supplier restock estimate: {supplier.group('date').strip()}.",
            "This is not a confirmed hospital delivery date.",
        )
    if "supplier restock date unknown" in text.lower():
        return (
            "Supplier restock estimate: unknown.",
            "No supplier restock date is available, so a hospital delivery date cannot be confirmed.",
        )
    inventory = re.search(r"observed available quantity=(?P<count>-?\d+)", text)
    if inventory:
        return (
            f"Observed inventory: {inventory.group('count')} units.",
            "This is an observed regional snapshot, not a reservation or substitution approval.",
        )
    if "no current inventory observation" in text.lower():
        return (
            "Inventory observation unavailable.",
            "No current regional observation was found; availability remains unknown.",
        )
    if text.startswith("CLARIFICATION_REQUIRED"):
        detail = text.split(":", 1)[1].strip() if ":" in text else "Please provide the missing detail."
        return "Please clarify.", detail
    if text.startswith("INSUFFICIENT_EVIDENCE"):
        return "Insufficient evidence.", text.split(".", 1)[1].strip() if "." in text else "Required evidence is missing."
    if text.startswith("ACCESS_DENIED"):
        return "Access denied.", text.split(":", 1)[1].strip() if ":" in text else "The requested evidence is outside the trusted scope."
    if text.startswith("Semantic discovery only"):
        return "Document evidence found.", "Semantic matches are discovery only and are not approval evidence."
    return "", text


def _supporting_lines(text: str) -> str:
    prefixes = (
        "Evaluated pair:",
        "Reasons:",
        "Guide:",
        "IFU:",
        "Contract evidence",
        "Applicable amendment",
        "Inventory:",
        "Backorder:",
        "Discovery source only",
    )
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(CITATION_PATTERN.sub("", raw_line).split())
        if line and line.startswith(prefixes):
            lines.append(line)
    return "\n\n".join(lines)


def parse_response(text: str) -> ResponseView:
    """Extract visual metadata without changing the agent's authoritative text."""
    outcome = next((candidate for candidate in OUTCOMES if text.startswith(candidate)), None)
    citations = _citations(text)
    candidate_outcomes = _candidate_outcomes(text)
    clean_lines = []
    for line in CITATION_PATTERN.sub("", text).splitlines():
        normalized = " ".join(line.split())
        if normalized:
            clean_lines.append(normalized)
    metrics = _metrics(text, include_substitution_inventory=not candidate_outcomes)
    if candidate_outcomes:
        summary_text = ""
        brief_explanation = "Semantic discovery identified exact SKU pairs; each outcome was checked deterministically. No candidate is clinically approved."
    elif outcome in {"BLOCKED", "ELIGIBLE_FOR_HUMAN_REVIEW", "INSUFFICIENT_EVIDENCE", "ACCESS_DENIED"}:
        summary_text, brief_explanation = _brief_substitution(outcome, text)
    else:
        summary_text, brief_explanation = _operational_summary(text, metrics)
    return ResponseView(
        text=text,
        display_text="\n\n".join(clean_lines),
        outcome=outcome,
        metrics=metrics,
        citations=citations,
        candidate_outcomes=candidate_outcomes,
        summary_text=summary_text,
        brief_explanation=brief_explanation,
        supporting_text=_supporting_lines(text),
    )
