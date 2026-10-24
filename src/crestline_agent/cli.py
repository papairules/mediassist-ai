"""Interactive local CLI for the MediAssist LangGraph agent."""

from __future__ import annotations

import argparse
from datetime import date
import os
import uuid

from crestline_retrieval.client import DatabricksBackend
from crestline_retrieval.models import AuthorizationContext
from crestline_retrieval.service import RetrievalService

from .graph import MediAssistAgent
from .llm import DatabricksIntentClassifier


DEFAULT_PROFILE = "abinroy90@gmail.com"
DEFAULT_WAREHOUSE_ID = "d249a9e5325ec021"
DEFAULT_VECTOR_INDEX = "workspace.crestline_silver.document_retrieval_chunks_index"
SYNTHETIC_AS_OF = date(2026, 10, 22)
DEMO_ONLY_SCOPES = {
    "east": ({"H-E101": "East", "H-E102": "East"}, frozenset({"East"})),
    "west": ({"H-W201": "West", "H-W202": "West"}, frozenset({"West"})),
    "central": ({"H-C301": "Central"}, frozenset({"Central"})),
}


def build_demo_only_authorization(scope: str) -> AuthorizationContext:
    """Build synthetic CLI scope; never use this function in a deployed application."""
    hospitals, regions = DEMO_ONLY_SCOPES[scope]
    return AuthorizationContext(
        principal_id=f"DEMO_ONLY_cli_{scope}",
        hospital_regions=hospitals,
        allowed_regions=regions,
        may_search_public_documents=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE_ID)
    parser.add_argument("--vector-index", default=DEFAULT_VECTOR_INDEX)
    parser.add_argument(
        "--llm-endpoint",
        default=os.environ.get("DATABRICKS_LLM_ENDPOINT"),
        help="Existing Databricks chat-model serving endpoint; or set DATABRICKS_LLM_ENDPOINT.",
    )
    parser.add_argument("--demo-scope", choices=sorted(DEMO_ONLY_SCOPES), default="east")
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--as-of", type=date.fromisoformat, default=SYNTHETIC_AS_OF)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not args.llm_endpoint:
        parser.error(
            "configure an existing endpoint with --llm-endpoint or DATABRICKS_LLM_ENDPOINT"
        )
    from databricks.sdk import WorkspaceClient

    workspace = WorkspaceClient(
        profile=args.profile,
        product="mediassist-local-agent",
        product_version="0.1.0",
    )
    service = RetrievalService(
        DatabricksBackend(workspace, args.warehouse_id),
        vector_index=args.vector_index,
    )
    classifier = DatabricksIntentClassifier(workspace, args.llm_endpoint)
    agent = MediAssistAgent()
    thread_id = args.thread_id or f"local-{uuid.uuid4()}"

    print(
        "MediAssist local chat — DEMO_ONLY synthetic authorization "
        f"scope={args.demo_scope}, thread_id={thread_id}."
    )
    print("This CLI is not a clinical approval system. Type /quit to exit.")
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message.lower() in {"/quit", "/exit"}:
            break
        if not message:
            continue
        # Rebuild from the trusted CLI scope on every turn. Checkpoint state is
        # conversational context only and never an authorization source.
        authorization = build_demo_only_authorization(args.demo_scope)
        reply = agent.invoke(
            message,
            thread_id=thread_id,
            service=service,
            authorization=authorization,
            classifier=classifier,
            as_of=args.as_of,
        )
        print(f"mediassist> {reply.text}")


if __name__ == "__main__":
    main()
