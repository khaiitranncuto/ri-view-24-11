import json
import unittest

from writer_contract import decode, reserve, schema


class WriterContractTests(unittest.TestCase):
    def events(self):
        return [dict(event_id='E1', sentence_quota=2,
            observed_facts=[dict(fact_id='F1')], sentence_slots=[
                dict(unit_id=uid, fact_ids=['F1'], visual_ids=['V1'],
                     shot_ids=['SHOT1'], source_start=0, source_end=5)
                for uid in ('U1', 'U2')])]

    def test_backend_binds_ids_in_locked_order(self):
        result = json.loads(decode('{"U2":"Second.","U1":"First."}', self.events()))
        self.assertEqual([r['unit_id'] for r in result['sentences']], ['U1', 'U2'])
        self.assertEqual(len(result['sentences']), 2)
        self.assertTrue(all(r['event_id'] == 'E1' for r in result['sentences']))

    def test_missing_and_empty_units_without_source_text_are_rejected(self):
        for value in ({'U1': 'One.'},
                      {'U1': 'One.', 'U2': ''}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(json.dumps(value), self.events())

    def test_invalid_plan_fails_before_generation(self):
        events = self.events()
        events[0]['sentence_quota'] = 3
        with self.assertRaisesRegex(ValueError, 'QUOTA'):
            schema(events)
        events = self.events()
        events[0]['sentence_slots'][0]['fact_ids'] = ['UNKNOWN']
        with self.assertRaisesRegex(ValueError, 'EVIDENCE'):
            schema(events)

    def test_shared_allowance_never_changes_quota(self):
        counts = {}
        events = self.events()
        for _ in range(4):
            reserve(events, counts, consume=False)
        self.assertEqual(counts, {})
        for _ in range(3):
            reserve(events, counts)
        with self.assertRaisesRegex(ValueError, 'BUDGET_EXHAUSTED'):
            reserve(events, counts)
        self.assertEqual(events[0]['sentence_quota'], 2)
        self.assertEqual(counts, {'U1': 3, 'U2': 3})
