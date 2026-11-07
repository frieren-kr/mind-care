"""Existing caregiver DB row -> Agent contract; synthetic values, no live DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import inspect
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from pydantic import ValidationError

from app.db import user_functions
from app.user_schemas import CaregiverProfileOut
from ..adapters.caregiver_profile import CaregiverProfileAdapter
from ..tools.caregiver_profile import CaregiverProfileReader
from .test_workflows import CONTEXT


def profile(context=CONTEXT, **updates):
    return CaregiverProfileOut(**{
        'user_id': context.user_id, 'relationship': '  등록된 관계\n지시문도 데이터  ',
        'burden_score': 48, 'mood_score': 12, 'lifestyle_tags': ['  직장병행  ', '수면부족', '수면부족'],
        'updated_at': datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9))), **updates,
    })


class CaregiverProfileAdapterTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_one_user_lookup_and_exact_projection_preserve_original_values_and_lists(self):
        self.assertEqual(inspect.signature(CaregiverProfileAdapter.get_caregiver_profile),
                         inspect.signature(CaregiverProfileReader.get_caregiver_profile))
        raw = profile()
        before = raw.model_dump(mode='json')
        lookup = Mock(return_value=raw)
        result = CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(context=CONTEXT)
        lookup.assert_called_once_with(CONTEXT.user_id)
        self.assertEqual(result.model_dump(mode='json'), {'profile': {k:v for k,v in before.items() if k!='user_id'}})
        self.assertEqual(result.profile.updated_at.utcoffset(), timedelta(hours=9))
        self.assertIsNot(result.profile.lifestyle_tags, raw.lifestyle_tags)
        result.profile.lifestyle_tags.append('changed Agent copy')
        self.assertEqual(raw.model_dump(mode='json'), before)

    def test_missing_row_and_present_nullable_profile_are_distinct_successes(self):
        missing = Mock(return_value=None)
        self.assertEqual(CaregiverProfileAdapter(get_caregiver_profile_fn=missing).get_caregiver_profile(
            context=CONTEXT).model_dump(), {'profile':None})
        missing.assert_called_once_with(CONTEXT.user_id)
        raw = profile(relationship=None, burden_score=None, mood_score=None, lifestyle_tags=[])
        lookup = Mock(return_value=raw)
        result = CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(context=CONTEXT)
        self.assertIsNotNone(result.profile)
        self.assertEqual((result.profile.relationship, result.profile.burden_score, result.profile.mood_score), (None,None,None))
        self.assertEqual(result.profile.lifestyle_tags, [])
        lookup.assert_called_once_with(CONTEXT.user_id)

    def test_patient_selection_does_not_change_user_query_or_returned_profile(self):
        lookup = Mock(return_value=profile())
        adapter = CaregiverProfileAdapter(get_caregiver_profile_fn=lookup)
        for patient_id in (None, CONTEXT.patient_id, UUID(int=987)):
            result = adapter.get_caregiver_profile(context=CONTEXT.model_copy(update={'patient_id':patient_id}))
            self.assertEqual(result.profile.burden_score,48)
        self.assertEqual([c.args for c in lookup.call_args_list], [(CONTEXT.user_id,)]*3)

    def test_foreign_user_row_is_rejected_before_profile_content_is_returned(self):
        lookup = Mock(return_value=profile(user_id=UUID(int=988)))
        with self.assertRaisesRegex(LookupError, '^Caregiver profile unavailable$'):
            CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(context=CONTEXT)
        lookup.assert_called_once_with(CONTEXT.user_id)

    def test_invalid_server_user_identity_is_rejected_without_query(self):
        for user_id in (None, str(CONTEXT.user_id), 'untrusted'):
            lookup = Mock(side_effect=AssertionError('No lookup without server user identity'))
            with self.subTest(user_id=user_id), self.assertRaises(LookupError):
                CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(
                    context=CONTEXT.model_copy(update={'user_id':user_id}))
            lookup.assert_not_called()

    def test_db_errors_propagate_without_retry_or_missing_profile_fallback(self):
        for error in (RuntimeError('PRIVATE DB detail'), TimeoutError('PRIVATE timeout')):
            lookup = Mock(side_effect=error)
            with self.subTest(error=type(error)), self.assertRaises(type(error)) as caught:
                CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(context=CONTEXT)
            self.assertIs(caught.exception,error)
            lookup.assert_called_once_with(CONTEXT.user_id)

    def test_out_of_range_or_unchecked_source_types_fail_without_clamping(self):
        invalid = [('burden_score',-1),('burden_score',89),('mood_score',28),
                   ('burden_score',True),('mood_score','12'),('mood_score',12.0),
                   ('updated_at',datetime(2026,10,1)),('lifestyle_tags',None),('lifestyle_tags','tag')]
        for field,value in invalid:
            raw = profile().model_copy(update={field:value})
            lookup = Mock(return_value=raw)
            with self.subTest(field=field,value=value), self.assertRaises(ValidationError):
                CaregiverProfileAdapter(get_caregiver_profile_fn=lookup).get_caregiver_profile(context=CONTEXT)
            lookup.assert_called_once_with(CONTEXT.user_id)

    def test_default_source_is_resolved_lazily_and_called_once(self):
        with patch.object(user_functions,'get_caregiver_profile',return_value=profile()) as lookup:
            adapter = CaregiverProfileAdapter()
            lookup.assert_not_called()
            result = adapter.get_caregiver_profile(context=CONTEXT)
        lookup.assert_called_once_with(CONTEXT.user_id)
        self.assertIsNone(adapter.get_caregiver_profile_fn)
        self.assertEqual(result.profile.mood_score,12)

    def test_shared_adapter_never_caches_or_crosses_user_rows(self):
        contexts = [CONTEXT.model_copy(update={'user_id':UUID(int=100+i),'patient_id':None}) for i in range(8)]
        rows = {c.user_id:profile(c,relationship=f'Synthetic {i}') for i,c in enumerate(contexts)}
        lookup = Mock(side_effect=lambda key:rows[key])
        adapter = CaregiverProfileAdapter(get_caregiver_profile_fn=lookup)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda c:adapter.get_caregiver_profile(context=c),contexts))
        self.assertEqual([r.profile.relationship for r in results],[f'Synthetic {i}' for i in range(8)])
        self.assertEqual(lookup.call_count,8)
        results[0].profile.lifestyle_tags.append('request-local change')
        self.assertNotIn('request-local change',results[1].profile.lifestyle_tags)
        self.assertNotIn('request-local change',rows[contexts[0].user_id].lifestyle_tags)
