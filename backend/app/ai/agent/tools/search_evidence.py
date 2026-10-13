"""Workflow-independent search Handler over the existing Retriever Protocol."""
from ..schemas import AgentContext
from .contracts import EvidencePackage, EvidenceSearchInput
from .evidence_tools import EvidenceRetriever


class SearchEvidenceHandler:
    """Forward Executor-validated arguments; Executor owns errors and timeouts."""

    def __init__(self, retriever: EvidenceRetriever):
        self.retriever = retriever

    def __call__(self, context: AgentContext, args: EvidenceSearchInput) -> EvidencePackage:
        return self.retriever.search_evidence(query=args.query, top_k=args.top_k, context=context)
