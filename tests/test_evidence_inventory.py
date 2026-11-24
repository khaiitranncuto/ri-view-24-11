import unittest

from evidence_inventory import detail_windows, inventory, detail_review_record, detail_stop_reason, observation_targets
from narrative_contract import SentenceEvidenceShortage, allocate_sentences


class EvidenceInventoryTests(unittest.TestCase):
    def test_observation_targets_scale_with_sentence_quota_and_story_importance(self):
        outline = {'story_beats': [{'start_seconds': 60, 'end_seconds': 120, 'story_role': 'climax'}]}
        short = observation_targets([0, 60, 120], outline, 12)
        long = observation_targets([0, 60, 120], outline, 24)
        self.assertEqual(short, [(4, 7), (9, 18)])
        self.assertEqual(long, [(7, 14), (18, 35)])

    def test_review_prioritizes_sparse_key_beat_over_earlier_sparse_footage(self):
        chunks = [{'events': [
            {'start_seconds': 0, 'end_seconds': 60, 'grounded_facts': [{'fact_id': 'F1'}]},
            {'start_seconds': 60, 'end_seconds': 120, 'grounded_facts': [{'fact_id': 'F2'}]},
        ]}]
        outline = {'story_beats': [{'start_seconds': 60, 'end_seconds': 120, 'story_role': 'resolution'}]}
        windows = detail_windows(chunks, 120, 8, prefer_story=True, outline=outline)
        self.assertEqual([window['start_seconds'] for window in windows], [60, 0])

    def test_failed_review_remains_distinct_from_successful_empty_review(self):
        window = dict(start_seconds=60, end_seconds=120)
        failed = detail_review_record(window, status='failed', error=ValueError('incomplete JSON'))
        completed = detail_review_record(window, failed, status='no_new_evidence')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(completed['status'], 'no_new_evidence')
        self.assertEqual(completed['attempts'], 2)
        self.assertIsNone(completed['error'])
        self.assertEqual(failed['attempts'], 1)

    def test_stagnation_stops_reserve_without_lowering_required_target(self):
        self.assertIsNone(detail_stop_reason(332, 281, 5, 0))
        self.assertEqual(detail_stop_reason(332, 281, 6, 0), 'reserve_stalled')
        self.assertEqual(detail_stop_reason(212, 281, 6, 0), 'required_evidence_stalled')
        self.assertEqual(detail_stop_reason(332, 281, 0, 3), 'consecutive_failures')
        self.assertIsNone(detail_stop_reason(332, 281, 0, 2))

    def test_review_can_cover_all_sparse_intervals_and_resume_without_duplicates(self):
        chunks = [{'events': [{'start_seconds': 120, 'end_seconds': 420,
                   'grounded_facts': [{'fact_id': 'F1', 'shot_ids': ['SHOT_1']}]}]}]
        windows = detail_windows(chunks, 600, 281)
        self.assertEqual(len(windows), 10)
        self.assertTrue(all(w['end_seconds'] - w['start_seconds'] <= 60 for w in windows))
        self.assertEqual(windows[0]['reason'], 'missing_story_observations')
        reviewed = [[w['start_seconds'], w['end_seconds']] for w in windows[:4]]
        remaining = detail_windows(chunks, 600, 281, reviewed=reviewed)
        self.assertEqual(len(remaining), 6)
        self.assertFalse({tuple(row) for row in reviewed} & {
            (w['start_seconds'], w['end_seconds']) for w in remaining})
        self.assertEqual(inventory(chunks)['source_shots'], 1)
        self.assertEqual(detail_windows(chunks, 600, 1), [])

    def test_fact_policy_does_not_silently_lower_duration_contract(self):
        plan = {'events_by_part': [[{'observed_facts': [{'text': 'A'}]}]],
                'scene_contract': {}, 'evidence_capacity_policy': 'distinct_facts'}
        with self.assertRaisesRegex(SentenceEvidenceShortage, 'SENTENCE_EVIDENCE_SHORTAGE') as shortage:
            allocate_sentences(plan, 8, 160)
        self.assertEqual(shortage.exception.capacity, 2)
        self.assertNotIn('sentence_quota', plan['events_by_part'][0][0])


if __name__ == '__main__':
    unittest.main()
