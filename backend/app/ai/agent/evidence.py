"""Request-local source authority. No LLM, retrieval or recommendation policy.

Only this object owns original payloads (immutable JSON). Occurrences and
snapshots hold identities, never independently mutable copies of the source.
"""
import json
from dataclasses import dataclass, field
from typing import Sequence

from .outputs import (ChatAnswerV1, ChatDraft, EvidenceReference, ResolvedEvidence,
                      _json_object, _reject_constant)
from .schemas import Message
from .tools.contracts import EvidenceItem, EvidencePackage


@dataclass(frozen=True)
class EvidenceKey:
    tool_call_id: str
    evidence_index: int

    def reference(self) -> EvidenceReference:
        return EvidenceReference(tool_call_id=self.tool_call_id, evidence_index=self.evidence_index)


@dataclass(frozen=True)
class EvidenceSnapshot:
    channel: str
    version: int
    keys: tuple[EvidenceKey, ...]
    owner: object = field(repr=False)

    def __deepcopy__(self, memo):
        return self


@dataclass(frozen=True)
class EvidenceCandidate:
    key: EvidenceKey
    store: "EvidenceStore" = field(repr=False)

    @property
    def tool_call_id(self):
        return self.key.tool_call_id

    @property
    def evidence_index(self):
        return self.key.evidence_index

    @property
    def evidence(self):
        return self.store.item(self.key)

    @property
    def source_ref(self):
        return self.key.reference()


class EvidenceStore:
    def __init__(self):
        self._packages: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        self._keys: list[EvidenceKey] = []
        self._snapshots: dict[str, EvidenceSnapshot] = {}
        self._owner = object()

    def __deepcopy__(self, memo):
        # Feed stages selection state, not source authority. Originals are
        # immutable and append-only; all batch validation precedes registration.
        return self

    @property
    def call_ids(self) -> set[str]:
        return set(self._packages)

    def add_search_result(self, call_id: str, package: EvidencePackage, *,
                          provider_call_id: str | None = None) -> tuple[EvidenceCandidate, ...]:
        EvidenceReference(tool_call_id=call_id, evidence_index=0)
        provider_call_id = call_id if provider_call_id is None else provider_call_id
        if not isinstance(provider_call_id, str) or not provider_call_id.strip():
            raise ValueError("Invalid Provider call identity")
        if call_id in self._packages:
            raise ValueError("Evidence call already registered")
        checked = EvidencePackage.model_validate(package.model_dump(warnings=False))
        encoded = (provider_call_id, checked.query, tuple(item.model_dump_json() for item in checked.evidence))
        keys = tuple(EvidenceKey(call_id, i) for i in range(len(checked.evidence)))
        self._packages[call_id] = encoded
        self._keys.extend(keys)
        # A Chat snapshot includes every successful occurrence, including
        # guidelines without PMID. Feed alone applies its eligibility policy.
        self.snapshot("chat", tuple(self._keys))
        return tuple(EvidenceCandidate(key, self) for key in keys)

    def packages(self) -> list[EvidencePackage]:
        return [EvidencePackage(query=query, evidence=[EvidenceItem.model_validate_json(row) for row in rows])
                for _, query, rows in self._packages.values()]

    def provider_call_id(self, key: EvidenceKey) -> str:
        self.item(key)
        return self._packages[key.tool_call_id][0]

    def item(self, key: EvidenceKey) -> EvidenceItem:
        ref = key.reference()
        raw = self._packages.get(ref.tool_call_id)
        if raw is None:
            raise ValueError("Evidence call not registered")
        rows = raw[2]
        if ref.evidence_index >= len(rows):
            raise ValueError("Evidence position outside original")
        return EvidenceItem.model_validate_json(rows[ref.evidence_index])

    def snapshot(self, channel: str, keys: Sequence[EvidenceKey]) -> EvidenceSnapshot:
        keys = tuple(keys)
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate snapshot occurrence")
        for key in keys:
            self.item(key)
        previous = self._snapshots.get(channel)
        snapshot = EvidenceSnapshot(channel, previous.version + 1 if previous else 1, keys, self._owner)
        self._snapshots[channel] = snapshot
        return snapshot

    def current(self, channel: str) -> EvidenceSnapshot:
        if channel not in self._snapshots:
            return self.snapshot(channel, ())
        return self._snapshots[channel]

    def validate_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        if (not isinstance(snapshot, EvidenceSnapshot) or snapshot.owner is not self._owner
                or self._snapshots.get(snapshot.channel) is not snapshot):
            raise ValueError("Stale or foreign evidence snapshot")
        for key in snapshot.keys:
            self.item(key)

    def select(self, snapshot: EvidenceSnapshot, mask: Sequence[bool]) -> list[EvidenceReference]:
        self.validate_snapshot(snapshot)
        if len(mask) != len(snapshot.keys) or any(type(value) is not bool for value in mask):
            raise ValueError("Selection must match current snapshot")
        return [key.reference() for key, selected in zip(snapshot.keys, mask, strict=True) if selected]

    def resolve(self, refs: Sequence[EvidenceReference]) -> list[ResolvedEvidence]:
        seen, resolved = set(), []
        for ref in refs:
            ref = EvidenceReference.model_validate(ref.model_dump(warnings=False))
            key = EvidenceKey(ref.tool_call_id, ref.evidence_index)
            if key in seen:
                raise ValueError("Duplicate evidence reference")
            seen.add(key)
            item = self.item(key)
            resolved.append(ResolvedEvidence(source_ref=ref, **{
                name: getattr(item, name) for name in ResolvedEvidence.model_fields if name != 'source_ref'}))
        return resolved

    def chat_message(self, snapshot: EvidenceSnapshot) -> Message:
        self.validate_snapshot(snapshot)
        # Keep year/journal for research selection and interpretation; source
        # identifiers stay server-owned. All projected fields are untrusted data.
        rows = []
        for key in snapshot.keys:
            item = self.item(key)
            rows.append(item.model_dump(exclude={'pmid', 'doi', 'source_url'}))
        return Message(role='user', kind='evidence', content=json.dumps({
            'context_type':'untrusted_chat_candidates', 'candidate_count':len(rows),
            'candidate_version':snapshot.version,
            'candidates':rows}, ensure_ascii=False, allow_nan=False))

    def finalize_chat(self, text: str, snapshot: EvidenceSnapshot) -> tuple[ChatAnswerV1, list[ResolvedEvidence]]:
        self.validate_snapshot(snapshot)
        if snapshot.channel != 'chat':
            raise ValueError("Unexpected Chat snapshot channel")
        if not isinstance(text, str) or len(text) > 65536:
            raise ValueError("Invalid structured response size")
        draft = ChatDraft.model_validate(json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant))
        refs = self.select(snapshot, draft.selected)
        output = ChatAnswerV1(schema_version='1', response_type='chat', answer=draft.answer, citation_refs=refs)
        return output, self.resolve(refs)
