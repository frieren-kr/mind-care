"""Explicit production Tool assembly; no API registration or Workflow policy."""
from .adapters.caregiver_profile import CaregiverProfileAdapter
from .adapters.patient_profile import PatientProfileAdapter
from .adapters.rag_evidence import RagEvidenceRetriever
from .registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from .schemas import AgentContext
from .tools.caregiver_profile import CaregiverProfileHandler, CaregiverProfileReader
from .tools.evidence_tools import EvidenceRetriever
from .tools.patient_profile import PatientProfileHandler, PatientProfileReader
from .tools.search_evidence import SearchEvidenceHandler


def build_evidence_registry(*, retriever: EvidenceRetriever | None = None,
                            parallel_safe: bool = True) -> ToolRegistry:
    """Expose only canonical search_evidence with the supplied or real RAG adapter.

    Backend composition supplies its LLM client and injects this Registry into
    an existing execution callable. This factory does not activate any endpoint.
    """
    handler = SearchEvidenceHandler(RagEvidenceRetriever() if retriever is None else retriever)
    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler,
                                  parallel_safe=parallel_safe),), mode="production")


def build_chat_registry(context: AgentContext, *, retriever: EvidenceRetriever | None = None,
                        patient_reader: PatientProfileReader | None = None,
                        caregiver_reader: CaregiverProfileReader | None = None,
                        parallel_safe: bool = True) -> ToolRegistry:
    """Chat registry_factory: search, user caregiver profile and selected patient profile.

    Backend owns authentication and endpoint wiring. Injected readers must be
    read-only and safe for concurrent calls when parallel_safe is True.
    The existing search-only factory remains available for Feed.
    """
    search_registry = build_evidence_registry(retriever=retriever, parallel_safe=parallel_safe)
    specs = [search_registry.get(ToolName.SEARCH_EVIDENCE.value)]
    if context.patient_id is not None:
        reader = PatientProfileAdapter() if patient_reader is None else patient_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE],
            PatientProfileHandler(reader), parallel_safe=parallel_safe))
    caregiver = CaregiverProfileAdapter() if caregiver_reader is None else caregiver_reader
    specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.CAREGIVER_PROFILE],
        CaregiverProfileHandler(caregiver), parallel_safe=parallel_safe))
    return ToolRegistry(tuple(specs), mode="production")
