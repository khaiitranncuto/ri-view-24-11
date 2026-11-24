import copy
import json
import unittest

from event_cluster_writer import cluster_plan, media_cards, prepare_plan, prompt
from recap_visual_media import evidence_manifest
from narrative_first import MATCH_AFTER_WRITING_VERSION


class EventClusterWriterTests(unittest.TestCase):
    def test_writer_prompt_and_media_work_through_part_context_conversion(self):
        from recap_pipeline import build_part_story_context
        event = prepare_plan(dict(events_by_part=[[self.event(1, 72, 90)]]))['events_by_part'][0][0]
        plan = dict(events_by_part=[[], [event]], boundaries=[0, 60, 120],
                    characters=[], summaries=['', ''], story_beats_by_part=[[], []])
        context = build_part_story_context(plan, 2)
        local = context['required_events'][0]
        self.assertNotIn('start_seconds', local)
        self.assertEqual(local['source_region_start_seconds'], 72)
        self.assertEqual(local['source_region_end_seconds'], 90)
        self.assertIn('fact_ids', prompt([local], context))
        manifest = evidence_manifest(media_cards([local]))
        self.assertEqual(manifest[0]['source_start'], 72)
        self.assertEqual(manifest[-1]['source_end'], 90)

    def test_single_response_confirms_event_and_binds_written_paragraph_afterwards(self):
        from tests.test_narration_units import NarrationUnitTests
        from writer_contract import decode, schema
        from narration_units import unit_response
        from evidence_writer import write_batch
        plan = NarrationUnitTests().plan()
        plan['characters'] = [dict(id='P1', name='Kukuru')]
        event = prepare_plan(plan)['events_by_part'][0][0]
        event['event_cluster_writer_version'] = 1
        observations = [dict(fact_id=f['fact_id'], status='confirmed', text=f['text'], actor_ids=['P1'])
                        for f in event['observed_facts']]
        slot = event['sentence_slots'][0]
        response = {'_observations': [dict(event_id=event['event_id'], facts=observations)],
                    slot['unit_id']: dict(text='Kukuru nhảy ra khỏi tù rồi lao về phía nhóm bạn.',
                                         fact_ids=[f['fact_id'] for f in observations])}
        self.assertIn('_observations', schema([event])['required'])
        draft = unit_response(json.loads(decode(json.dumps(response), [event])), [event])
        rows = write_batch([event], {}, lambda *args: self.fail('Valid event should be reused'),
                           candidates=[draft], characters=plan['characters'], fast_mode=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['sentence_proofs']), 1)
        self.assertEqual(rows[0]['sentence_proofs'][0]['fact_ids'], ['F1', 'F2'])
        self.assertEqual(event['characters'], ['P1'])

    def test_invalid_observations_cannot_partially_replace_event_facts(self):
        from writer_contract import decode
        event = prepare_plan(dict(events_by_part=[[self.event(1, 0, 12)]]))['events_by_part'][0][0]
        original = copy.deepcopy(event)
        with self.assertRaises(ValueError):
            decode(json.dumps(dict(_observations=[dict(event_id='WRONG', facts=[])])), [event])
        self.assertEqual(event, original)

    def event(self, index, start, end, location='Bo song'):
        return dict(event_id=f'E{index}', scene_id=f'S{index}', start_seconds=start, end_seconds=end,
                    scene_context=dict(location=location, time_context='Dem', goal='Tro chuyen'),
                    story_role='development', story_beat_id='GB1', characters=['P1'],
                    observed_facts=[dict(fact_id=f'F{index}', text=f'Dien bien {index}.')],
                    planned_fact_ids=[f'F{index}'], visual_sources=[],
                    sentence_quota=1, sentence_slots=[dict(unit_id=f'U{index}')])

    def test_merge_same_event_preserves_all_facts_and_stops_at_new_location(self):
        original = dict(events_by_part=[[self.event(1, 0, 6), self.event(2, 6, 18),
                                        self.event(3, 18, 24, 'Nha tu')]], scene_contract={})
        result = cluster_plan(original)
        events = result['events_by_part'][0]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['cluster_source_event_ids'], ['E1', 'E2'])
        self.assertEqual(events[0]['planned_fact_ids'], ['F1', 'F2'])
        self.assertEqual(events[0]['end_seconds'], 18)
        self.assertEqual(len(original['events_by_part'][0]), 3)

    def test_media_covers_whole_event_without_gaps_or_duplicate_frames(self):
        event = self.event(1, 2.75, 29.5)
        manifest = evidence_manifest(media_cards([event]))
        self.assertEqual(manifest[0]['source_start'], 2.75)
        self.assertEqual(manifest[-1]['source_end'], 29.5)
        self.assertAlmostEqual(sum(r['source_end']-r['source_start'] for r in manifest), 26.75)
        self.assertTrue(all(a['source_end'] == b['source_start'] for a, b in zip(manifest, manifest[1:])))

    def test_writer_sees_event_without_locked_per_sentence_fact_assignments(self):
        plan = prepare_plan(dict(events_by_part=[[self.event(1, 0, 12)]]))
        event = plan['events_by_part'][0][0]
        self.assertEqual(event['narrative_first_version'], MATCH_AFTER_WRITING_VERSION)
        text = prompt([event], {})
        self.assertIn('fact_ids', text)
        self.assertNotIn('locked_facts', text)
        self.assertIn('fact_ids', text)

    def test_long_event_chain_splits_at_event_boundary(self):
        plan = dict(events_by_part=[[self.event(1, 0, 100), self.event(2, 100, 200)]], scene_contract={})
        self.assertEqual(len(cluster_plan(plan)['events_by_part'][0]), 2)
