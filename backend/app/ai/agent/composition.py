"""Explicit production search assembly; no API registration or Workflow policy."""
from .adapters.rag_evidence import RagEvidenceRetriever
from .registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from .tools.evidence_tools import EvidenceRetriever
from .tools.search_evidence import SearchEvidenceHandler


def build_evidence_registry(*, retriever: EvidenceRetriever | None = None) -> ToolRegistry:
    """Expose only canonical search_evidence with the supplied or real RAG adapter.

    Backend composition supplies its LLM client and injects this Registry into
    an existing execution callable. This factory does not activate any endpoint.
    """
    handler = SearchEvidenceHandler(RagEvidenceRetriever() if retriever is None else retriever)
    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler),), mode="production")
