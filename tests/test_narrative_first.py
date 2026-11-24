import copy
import json
import unittest

from narrative_first import (MATCH_AFTER_WRITING_VERSION, LOCKED_VERSION, bind_response,
                             completed_boundary_context, deferred_event_hint, prompt, schema)
from tests import test_narration_units
from evidence_writer import bind_planned_scenes
from narration_units import unit_response


class NarrativeFirstTests(unittest.TestCase):
    def test_locked_prompt_keeps_assigned_facts_and_removes_backend_geometry(self):
        from event_cluster_writer import prepare_plan
        plan = prepare_plan(test_narration_units.NarrationUnitTests().plan(), lock_evidence=True)
        event = plan['events_by_part'][0][0]
        event['observed_facts'].append(dict(fact_id='UNUSED', text='Unused source evidence.'))
        event['identity_contract'] = {'visible_names': ['Kukuru'], 'evidence': ['duplicate source']}
        text = prompt([event], {})
        packet = json.loads(text[text.index('{"outline"'):])
        card = packet['events'][0]
        self.assertNotIn('visual_draft', card)
        self.assertNotIn('evidence', card['identity'])
        self.assertEqual(card['identity']['visible_names'], ['Kukuru'])
        self.assertNotIn('UNUSED', [fact['fact_id'] for fact in card['facts']])
        self.assertEqual(set(card['locked_facts'][event['sentence_slots'][0]['unit_id']]),
                         {fact['fact_id'] for fact in card['facts']})

    def test_locked_event_plan_uses_backend_evidence_and_recovers_missing_prose(self):
        from event_cluster_writer import prepare_plan
        from evidence_writer import write_batch
        from flat_writer import flat_to_paragraphs
        plan = prepare_plan(test_narration_units.NarrationUnitTests().plan(),
                            lock_evidence=True)
        event = plan['events_by_part'][0][0]
        before = copy.deepcopy(event['sentence_slots'])
        calls = []
        def generate(*args):
            calls.append(True)
            return json.dumps({'sentences': []})
        rows = write_batch([event], {}, generate, fast_mode=True,
                           prompt_builder=prompt, response_adapter=flat_to_paragraphs)
        self.assertEqual(len(calls), 2)
        self.assertEqual(event['narrative_first_version'], LOCKED_VERSION)
        self.assertEqual(event['sentence_slots'], before)
        self.assertEqual(rows[0]['narration_recovery']['method'], 'locked_source_facts')

    def test_backend_reconsiders_early_shot_to_preserve_later_sentence(self):
        from narrative_first import ordered_visual_assignment
        units = [{'unit_id': 'U1'}, {'unit_id': 'U2'}]
        visuals = [dict(visual_id='early', start_seconds=0, end_seconds=10,
                        fact_ids=['A'], shot_ids=['S1']),
                   dict(visual_id='shared', start_seconds=3, end_seconds=5,
                        fact_ids=['A', 'B'], shot_ids=['S2'])]
        # The early long shot blocks B. Select the shared shot for both sentences.
        value = {'U1': {'fact_ids': ['A']}, 'U2': {'fact_ids': ['B']}}
        assignment = ordered_visual_assignment(units, value,
            {'A': {}, 'B': {}}, visuals, 'E1')
        self.assertIsNotNone(assignment)
        self.assertEqual([v['visual_id'] for v in assignment[1]], ['shared'])

    def test_extend_end_to_next_observation_only_in_same_conversation(self):
        from narrative_first import event_region_windows
        from video_renderer import build_visual_edit_plan
        events = [dict(event_id='E85', start_seconds=3864, end_seconds=3866.75,
                       scene_context=dict(location='Bo song', time_context='Hoang hon'),
                       sentence_slots=[dict(source_start=3864, source_end=3866.75)]),
                  dict(event_id='E86', start_seconds=3882, end_seconds=3888,
                       scene_context=dict(location='Bo song', time_context='Hoang hon'),
                       transition_contract=dict(relation='continuation'),
                       sentence_slots=[dict(source_start=3882, source_end=3888)])]
        regions = event_region_windows(events)
        self.assertEqual(regions['E85']['region_end'], 3882)
        shot = dict(start=3864, end=3866.75, anchors=[], spans=[[3864, 3866.75]], **regions['E85'])
        edit = build_visual_edit_plan([5], [3864], [3866.75], 6234,
                                     visual_draft_shots=[shot])[0]
        self.assertEqual(edit['source_start'], 3864)
        self.assertEqual(edit['source_duration'], 5)
        self.assertEqual(edit['hold_duration'], 0)
        events[1]['scene_context']['location'] = 'Hang dong'
        self.assertEqual(event_region_windows(events)['E85']['region_end'], 3866.75)

    def test_tts_reallocates_neighbouring_shots_inside_event_without_freezing(self):
        from video_renderer import build_visual_edit_plan
        shots = [dict(start=2, end=4, anchors=[3], spans=[[2, 4]],
                      region_id='E1', region_start=0, region_end=20),
                 dict(start=6, end=10, anchors=[8], spans=[[6, 10]],
                      region_id='E1', region_start=0, region_end=20)]
        edits = build_visual_edit_plan([4, 8], [2, 6], [4, 10], 30,
                                      visual_draft_shots=shots,
                                      strict_visual_lock=True, extend_to_next_shot=True)
        self.assertTrue(all(edit['hold_duration'] == 0 for edit in edits))
        previous = 0
        for edit, anchor in zip(edits, [3, 8]):
            start, duration = edit['shot_spans'][0]
            self.assertGreaterEqual(start, previous)
            self.assertLessEqual(start, anchor)
            self.assertGreater(start + duration, anchor)
            self.assertLessEqual(start + duration, 20)
            previous = start + duration

    def test_region_exhaustion_is_detected_before_cutting(self):
        from video_renderer import build_visual_edit_plan
        shot = dict(start=2, end=4, anchors=[3], spans=[[2, 4]],
                    region_id='E1', region_start=0, region_end=5)
        with self.assertRaisesRegex(ValueError, 'EVENT_REGION_FOOTAGE_SHORTAGE'):
            build_visual_edit_plan([6], [2], [4], 30, visual_draft_shots=[shot])

    def test_deferred_event_gives_successor_a_locked_story_hint(self):
        event = self.event()
        event.update(state_before='Dang bi bao vay.', state_after='Da thoat khoi vong vay.',
                     story_beat_summary='Nhan vat pha vong vay.')
        hint = deferred_event_hint(event)
        self.assertTrue(hint['pending_text'])
        self.assertEqual(hint['event_id'], event['event_id'])
        self.assertEqual(hint['state_after'], 'Da thoat khoi vong vay.')
        self.assertEqual([row['unit_id'] for row in hint['planned_units']],
                         [row['unit_id'] for row in event['sentence_slots']])

    def test_final_retry_uses_real_written_neighbours_not_completion_order(self):
        events = [dict(event_id=f'E{i:04d}') for i in range(1, 7)]
        written = [dict(event_id='E0006', tts='Sau nua.'),
                   dict(event_id='E0002', tts='Truoc sat.'),
                   dict(event_id='E0004', tts='Sau sat.'),
                   dict(event_id='E0001', tts='Truoc nua.'),
                   dict(event_id='E0005', tts='Sau ke.')]
        before, after = completed_boundary_context(events, written, {'E0003'}, width=2)
        self.assertEqual([row['event_id'] for row in before], ['E0001', 'E0002'])
        self.assertEqual([row['event_id'] for row in after], ['E0004', 'E0005'])

    def test_first_pass_moments_survive_short_narration_at_render(self):
        from grounded_pipeline import bind_observations, shot_index
        from narrative_first import visual_first_plan
        from video_renderer import build_visual_edit_plan

        raw = {'events': [{'event': 'Tia sét rời gậy rồi đánh trúng.', 'content_kind': 'story', 'observations': [
            dict(text='Tia sét rời gậy rồi đánh trúng.', shot_ids=['SHOT_000001'],
                 key_moments=[dict(shot_id='SHOT_000001', offset_seconds=1.2),
                              dict(shot_id='SHOT_000001', offset_seconds=5.1),
                              dict(shot_id='SHOT_000002', offset_seconds=1.0)])]}]}
        fact = bind_observations(raw, shot_index(12), 0, 12, 1)['events'][0]['grounded_facts'][0]
        self.assertEqual(fact['visual_anchor_seconds'], [1.2, 5.1])

        plan = test_narration_units.NarrationUnitTests().plan()
        event = plan['events_by_part'][0][0]
        event['observed_facts'][0]['visual_anchor_seconds'] = fact['visual_anchor_seconds']
        plan['draft_target_seconds'] = 4
        draft = visual_first_plan(plan)['visual_draft']['clips'][0]['shots']
        edit = build_visual_edit_plan([2.5], [0], [6], 12, visual_draft_shots=draft)[0]
        self.assertEqual(len(edit['shot_spans']), 2)
        self.assertAlmostEqual(edit['source_duration'], 2.5)
        for moment in fact['visual_anchor_seconds']:
            self.assertTrue(any(start <= moment < start + length for start, length in edit['shot_spans']))

    def test_visual_first_freezes_timeline_before_writer_and_binds_locked_text(self):
        from narrative_first import visual_first_plan
        from writer_contract import decode
        from flat_writer import flat_to_paragraphs
        original = test_narration_units.NarrationUnitTests().plan()
        original['draft_target_seconds'] = 10
        plan = visual_first_plan(original)
        self.assertNotIn('visual_draft', original)
        event = plan['events_by_part'][0][0]
        self.assertEqual(event['narrative_first_version'], LOCKED_VERSION)
        self.assertEqual(len(plan['visual_draft']['clips']), 1)
        self.assertAlmostEqual(plan['visual_draft']['duration'], 10)
        unit = event['sentence_slots'][0]
        self.assertEqual(event['visual_draft'][0]['shot_ids'], unit['shot_ids'])
        before = copy.deepcopy(event['sentence_slots'])
        draft = json.dumps({unit['unit_id']: 'Kukuru nhảy khỏi tủ rồi lao về phía nhóm bạn.'})
        raw = decode(draft, [event])
        scene = bind_planned_scenes(flat_to_paragraphs(json.loads(raw), [event]), [event])[0]
        self.assertEqual(event['sentence_slots'], before)
        self.assertEqual(scene['sentence_proofs'][0]['fact_ids'], unit['fact_ids'])
        self.assertIn('region_start/region_end', prompt([event], {}))

    def event(self):
        event = test_narration_units.NarrationUnitTests().plan()['events_by_part'][0][0]
        event['narrative_first_version'] = 'narrative_first_v1'
        return event

    def test_write_first_then_reassign_visuals(self):
        event = self.event()
        uid = event['sentence_slots'][0]['unit_id']
        raw = bind_response({uid: dict(text=event['observed_facts'][1]['text'],
                                      fact_ids=['F2'])}, [event])
        self.assertEqual(event['sentence_slots'][0]['fact_ids'], ['F2'])
        self.assertEqual(event['sentence_slots'][0]['shot_ids'], ['SHOT_000002'])
        rows = bind_planned_scenes(unit_response(json.loads(raw), [event]), [event])
        self.assertEqual(len(rows[0]['sentence_proofs']), 1)
        self.assertEqual(rows[0]['sentence_proofs'][0]['fact_ids'], ['F2'])

    def test_writer_batch_binds_draft_and_restores_saved_assignment(self):
        from evidence_writer import write_batch
        from flat_writer import flat_to_paragraphs
        from writer_contract import decode

        event = self.event()
        event['narrative_first_version'] = MATCH_AFTER_WRITING_VERSION
        original = copy.deepcopy(event)
        uid = event['sentence_slots'][0]['unit_id']
        draft = json.dumps({uid: {'text': event['observed_facts'][1]['text'],
                                  'fact_ids': ['F2']}})
        saved = []

        def generate(events, _):
            raw = decode(draft, events)
            self.assertEqual(events[0], original)
            return raw

        def unexpected_generate(*args):
            self.fail('The saved draft should be rebound without a provider call')

        rows = write_batch([event], {}, generate, prompt_builder=prompt,
                           response_adapter=flat_to_paragraphs,
                           save=lambda drafts: saved.extend(copy.deepcopy(drafts)), fast_mode=True)
        self.assertEqual(rows[0]['sentence_proofs'][0]['shot_ids'], ['SHOT_000002'])
        recovered = write_batch([original], {}, unexpected_generate, candidates=saved,
                                response_adapter=flat_to_paragraphs, fast_mode=True)
        self.assertEqual(recovered, rows)

    def test_fast_writer_repairs_only_missing_or_unbound_unit_on_resume(self):
        from evidence_writer import write_batch
        from flat_writer import flat_to_paragraphs
        from narration_units import plan_narration_units
        from writer_contract import decode

        for invalid_visual in (False, True):
            with self.subTest(invalid_visual=invalid_visual):
                event = self.event()
                event['narrative_first_version'] = MATCH_AFTER_WRITING_VERSION
                event['sentence_quota'] = 2
                event = plan_narration_units({'events_by_part': [[event]]})['events_by_part'][0][0]
                first, second = [slot['unit_id'] for slot in event['sentence_slots']]
                first_text, second_text = [fact['text'] for fact in event['observed_facts']]
                event['observed_facts'].append(dict(fact_id='F3',
                    text='Kukuru dung lai.', shot_ids=['SHOT_MISSING']))
                original = copy.deepcopy(event)
                draft = {first: dict(text=first_text, fact_ids=['F1'])}
                if invalid_visual:
                    draft[second] = dict(text='Kukuru dung lai.', fact_ids=['F3'])
                saved = []
                requests = []

                def generate(events, request_prompt):
                    ids = [slot['unit_id'] for slot in events[0]['sentence_slots']]
                    requests.append(ids)
                    if len(ids) == 2:
                        self.assertNotIn('"fact_id":"F3"', request_prompt)
                        return decode(json.dumps(draft), events)
                    raise ConnectionError('provider interrupted')

                options = dict(prompt_builder=prompt, response_adapter=flat_to_paragraphs,
                               fast_mode=True)
                with self.assertRaises(ConnectionError):
                    write_batch([event], {}, generate, save=saved.append, **options)
                self.assertEqual(requests, [[first, second], [second]])

                def resume(events, _):
                    self.assertEqual([s['unit_id'] for s in events[0]['sentence_slots']], [second])
                    return decode(json.dumps({second: dict(text=second_text, fact_ids=['F2'])}), events)

                scene, = write_batch([original], {}, resume, candidates=saved[-1], **options)
                self.assertEqual([proof['text'] for proof in scene['sentence_proofs']],
                                 [first_text, second_text])
                self.assertEqual([proof['fact_ids'] for proof in scene['sentence_proofs']],
                                 [['F1'], ['F2']])

    def test_invalid_draft_cannot_change_visual_plan(self):
        event = self.event()
        original = copy.deepcopy(event)
        uid = event['sentence_slots'][0]['unit_id']
        for value in [{}, {uid: dict(text='C?u ch?y.', fact_ids=['UNKNOWN'])},
                      {uid: dict(text='C?u ch?y. C?u ng?.', fact_ids=['F1'])}]:
            with self.assertRaises(ValueError):
                bind_response(value, [event])
            self.assertEqual(event, original)

    def test_locked_draft_returns_text_with_backend_owned_visuals(self):
        event = self.event()
        event['narrative_first_version'] = LOCKED_VERSION
        original = copy.deepcopy(event)
        unit = event['sentence_slots'][0]
        uid = unit['unit_id']
        fact = next(f for f in event['observed_facts'] if f['fact_id'] == unit['fact_ids'][0])
        self.assertEqual(schema([event])['properties'][uid], {'type': 'string'})
        self.assertIn('locked_facts', prompt([event], {}))
        raw = bind_response({uid: fact['text']}, [event])
        rows = bind_planned_scenes(unit_response(json.loads(raw), [event]), [event])
        self.assertEqual(rows[0]['sentence_proofs'][0]['shot_ids'], unit['shot_ids'])
        self.assertEqual(event, original)

    def test_prompt_has_story_context_without_locked_shots(self):
        event = self.event()
        event.update(
            spoken_text='We need a place where nobody lives.',
            story_beat_summary='Cả nhóm tìm nơi ở mới.',
            story_beat_cause='Không còn đất trống ở hiện tại.',
            story_beat_goal='Tìm vùng đất tự do.',
            story_beat_outcome='Họ quyết định quay về thời tiền sử.',
            story_beat_opening=True,
            story_beat_closing=True,
        )
        text = prompt([event], dict(previous_part_tail=[dict(tts='?o?n tr??c.')],
            next_batch_context=[dict(event='?o?n sau.')]))
        self.assertIn('?o?n tr??c.', text)
        self.assertIn('?o?n sau.', text)
        self.assertNotIn('SHOT_000001', text)
        self.assertNotIn('visual_ids', text)
        self.assertIn('Không còn đất trống ở hiện tại.', text)
        self.assertIn('We need a place where nobody lives.', text)
        self.assertIn('transcript_context_only', text)
        self.assertEqual(schema([event])['required'],
                         [event['sentence_slots'][0]['unit_id']])
