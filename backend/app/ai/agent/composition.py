"""Explicit production Tool assembly; no API registration or Workflow policy."""
from .adapters.caregiver_profile import CaregiverProfileAdapter
from .adapters.patient_profile import PatientProfileAdapter
from .adapters.patient_medications import PatientMedicationsAdapter
from .adapters.patient_medical_visits import PatientMedicalVisitsAdapter
from .adapters.patient_assessments import PatientAssessmentsAdapter
from .adapters.patient_safety_events import PatientSafetyEventsAdapter
from .adapters.recent_care_logs import RecentCareLogsAdapter
from .adapters.rag_evidence import RagEvidenceRetriever
from .adapters.rag_guidelines import RagGuidelineRetriever
from .registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from .schemas import AgentContext
from .tools.caregiver_profile import CaregiverProfileHandler, CaregiverProfileReader
from .tools.evidence_tools import EvidenceRetriever
from .tools.guidelines import GuidelineRetriever, SearchGuidelinesHandler
from .tools.patient_profile import PatientProfileHandler, PatientProfileReader
from .tools.medications import PatientMedicationsHandler, PatientMedicationsReader
from .tools.medical_visits import PatientMedicalVisitsHandler, PatientMedicalVisitsReader
from .tools.assessments import PatientAssessmentsHandler, PatientAssessmentsReader
from .tools.safety_events import PatientSafetyEventsHandler, PatientSafetyEventsReader
from .tools.recent_care_logs import RecentCareLogsHandler, RecentCareLogsReader
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
                        guidelines_retriever: GuidelineRetriever | None = None,
                        patient_reader: PatientProfileReader | None = None,
                        caregiver_reader: CaregiverProfileReader | None = None,
                        care_logs_reader: RecentCareLogsReader | None = None,
                        medications_reader: PatientMedicationsReader | None = None,
                        visits_reader: PatientMedicalVisitsReader | None = None,
                        assessments_reader: PatientAssessmentsReader | None = None,
                        safety_events_reader: PatientSafetyEventsReader | None = None,
                        parallel_safe: bool = True) -> ToolRegistry:
    """Chat registry_factory: paper/guideline search and scoped profile/read Tools.

    Backend owns authentication and endpoint wiring. Injected readers must be
    read-only and safe for concurrent calls when parallel_safe is True.
    The existing search-only factory remains available for Feed.
    """
    search_registry = build_evidence_registry(retriever=retriever, parallel_safe=parallel_safe)
    specs = [search_registry.get(ToolName.SEARCH_EVIDENCE.value)]
    guidelines = RagGuidelineRetriever() if guidelines_retriever is None else guidelines_retriever
    specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_GUIDELINES],
        SearchGuidelinesHandler(guidelines), parallel_safe=parallel_safe))
    if context.patient_id is not None:
        reader = PatientProfileAdapter() if patient_reader is None else patient_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE],
            PatientProfileHandler(reader), parallel_safe=parallel_safe))
        # The default logs Adapter deliberately fails until its DB query is bound.
        logs = RecentCareLogsAdapter() if care_logs_reader is None else care_logs_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.RECENT_CARE_LOGS],
            RecentCareLogsHandler(logs), parallel_safe=parallel_safe))
        medications = PatientMedicationsAdapter() if medications_reader is None else medications_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS],
            PatientMedicationsHandler(medications), parallel_safe=parallel_safe))
        visits = PatientMedicalVisitsAdapter() if visits_reader is None else visits_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_MEDICAL_VISITS],
            PatientMedicalVisitsHandler(visits), parallel_safe=parallel_safe))
        assessments = PatientAssessmentsAdapter() if assessments_reader is None else assessments_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_ASSESSMENTS],
            PatientAssessmentsHandler(assessments), parallel_safe=parallel_safe))
        safety_events = PatientSafetyEventsAdapter() if safety_events_reader is None else safety_events_reader
        specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_SAFETY_EVENTS],
            PatientSafetyEventsHandler(safety_events), parallel_safe=parallel_safe))
    caregiver = CaregiverProfileAdapter() if caregiver_reader is None else caregiver_reader
    specs.append(ToolSpec(TOOL_CONTRACTS[ToolName.CAREGIVER_PROFILE],
        CaregiverProfileHandler(caregiver), parallel_safe=parallel_safe))
    return ToolRegistry(tuple(specs), mode="production")
