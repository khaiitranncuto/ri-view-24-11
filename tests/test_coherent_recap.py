import json
import unittest

from coherent_recap import one_sentence, recover_events, story_outline, story_prompt
from coherent_recap import restore_locked_structure
from tests import test_narration_units
from narrative_contract import sentences
from writer_contract import decode
from source_grounded_writer import is_source_grounded
from grounded_pipeline import apply_local_visual_fallbacks
from sentence_visual_contract import enforce_sentence_visual_contract


class CoherentRecapTests(unittest.TestCase):
    def test_whole_film_corrects_named_subject_using_the_same_visual_action(self):
        from coherent_recap import bind_whole_film_response
        fixture = test_narration_units.NarrationUnitTests()
        event = fixture.plan()['events_by_part'][0][0]
        fact = event['observed_facts'][0]
        fact['text'] = 'Nobita mở cửa rồi bước vào trong.'
        response = fixture.response()
        response['sentences'][0]['text'] = 'Kukuru mở cửa rồi bước vào trong.'
        people = [{'name': 'Nobita'}, {'name': 'Kukuru'}]
        scene = bind_whole_film_response(json.dumps(response), [event], people)[0]
        self.assertEqual(scene['tts'], fact['text'])

    def test_whole_film_edit_salvages_complete_rows_from_truncated_json(self):
        from coherent_recap import bind_whole_film_edit, bind_whole_film_response
        fixture = test_narration_units.NarrationUnitTests()
        event = fixture.plan()['events_by_part'][0][0]
        raw = fixture.response()
        original = bind_whole_film_response(json.dumps(raw), [event], [])
        uid = raw['sentences'][0]['unit_id']
        repaired = 'Nobita mở cửa rồi bước vào trong.'
        response = '{"sentences":[' + json.dumps({'unit_id': uid, 'text': repaired}) + ',{"unit_id":"cut'
        partial = []
        rows = bind_whole_film_edit(response, original, [event], [], 20, on_partial=partial.append)
        self.assertEqual(partial, [1])
        self.assertEqual(rows[0]['tts'], repaired)
        self.assertTrue(is_source_grounded(rows[0]))

    def test_whole_film_edit_keeps_locked_evidence_and_rejects_unknown_ids(self):
        from coherent_recap import bind_whole_film_edit, bind_whole_film_response, whole_film_edit_prompt
        fixture = test_narration_units.NarrationUnitTests()
        event = fixture.plan()['events_by_part'][0][0]
        raw = fixture.response()
        original = bind_whole_film_response(json.dumps(raw), [event], [])
        self.assertIn('mục tiêu 5150 từ', whole_film_edit_prompt('Kế hoạch', original, 5150))
        row = dict(raw['sentences'][0], text='Nobita mở cửa rồi bước vào trong.')
        edited = bind_whole_film_edit(json.dumps({'sentences': [row]}), original, [event], [], 20)
        self.assertEqual(edited[0]['tts'], row['text'])
        self.assertEqual(edited[0]['sentence_proofs'][0]['shot_ids'],
                         original[0]['sentence_proofs'][0]['shot_ids'])
        with self.assertRaisesRegex(ValueError, 'WHOLE_FILM_EDIT_UNIT_INVALID'):
            bind_whole_film_edit(json.dumps({'sentences': [dict(row, unit_id='unknown')]}),
                                 original, [event], [], 20)

    def test_whole_film_repairs_quoted_item_names_before_binding(self):
        from coherent_recap import bind_whole_film_response
        fixture = test_narration_units.NarrationUnitTests()
        event = fixture.plan()['events_by_part'][0][0]
        for name, expected in (
            ('“Chiếc đèn thần”', 'Chiếc đèn thần'),
            ('"Magic Lamp"', 'bảo bối để soi đường'),
        ):
            with self.subTest(name=name):
                response = fixture.response()
                response['sentences'][0]['text'] = f'Nobita dùng bảo bối {name} để soi đường.'
                scene = bind_whole_film_response(json.dumps(response), [event], [])[0]
                self.assertIn(expected, scene['tts'])
                self.assertNotIn(name, scene['tts'])
                self.assertEqual(len(scene['sentence_proofs']), 1)
                self.assertTrue(is_source_grounded(scene))

    def test_whole_film_writer_requires_every_unit_and_keeps_locked_shots(self):
        from coherent_recap import bind_whole_film_response, whole_film_prompt
        fixture = test_narration_units.NarrationUnitTests()
        event = fixture.plan()['events_by_part'][0][0]
        self.assertIn(event['sentence_slots'][0]['unit_id'], whole_film_prompt([event], {}))
        second = dict(event, event_id='E0002')
        with self.assertRaisesRegex(ValueError, 'WRITER_PLAN_EVIDENCE_INVALID|WHOLE_FILM_BRIDGE_SLOT_INVALID'):
            whole_film_prompt([event, second], {})
        response = fixture.response()
        scenes = bind_whole_film_response(json.dumps(response), [event], [])
        self.assertTrue(is_source_grounded(scenes[0]))
        self.assertEqual(len(scenes[0]['locked_visual_shots']), 2)
        padded_rows = [dict(row, unit_id=' ' + row['unit_id'] + ' ')
                       for row in reversed(response['sentences'])]
        self.assertEqual(bind_whole_film_response(
            json.dumps({'sentences': padded_rows}), [event], []), scenes)
        with self.assertRaisesRegex(ValueError, r'duplicate=\[.+\]'):
            bind_whole_film_response(json.dumps({
                'sentences': padded_rows + [padded_rows[0]],
            }), [event], [])
        with self.assertRaisesRegex(ValueError, 'WHOLE_FILM_UNIT_ORDER_INVALID'):
            bind_whole_film_response('{"sentences":[]}', [event], [])

    def test_word_budget_follows_evidence_detail_and_preserves_total(self):
        from narrative_contract import allocate_sentences
        events = [dict(event_id='short', visual_sources=[], observed_facts=[
            dict(fact_id='F1', text='Kukuru bat ca.', shot_ids=['S1'])]),
            dict(event_id='detailed', visual_sources=[], observed_facts=[
                dict(fact_id='F2', text='Kukuru lao vao can cu, doi mat ke thu va be gay quyen truong de giai cuu dong doi.', shot_ids=['S2'])])]
        plan = dict(events_by_part=[events], scene_contract={}, situation_plan_version=4,
                    evidence_capacity_policy='distinct_facts', editorial_story_spine=[dict(summary='Mach phim.')])
        result = allocate_sentences(plan, 2, 40)
        first, second = result['events_by_part'][0]
        self.assertLess(first['tts_target_words'], second['tts_target_words'])
        self.assertEqual(first['tts_target_words'] + second['tts_target_words'], 40)
        self.assertEqual(first['sentence_quota'] + second['sentence_quota'], 2)

    def test_editorial_phase_prioritizes_final_climax_and_keeps_ending_short(self):
        from coherent_recap import editorial_plan
        from narrative_contract import allocate_sentences
        events = [dict(event_id=phase, observed_facts=[
            dict(fact_id=f'{phase}{i}', text=f'Dien bien {i} co ket qua.', shot_ids=[f'S{i}'])
            for i in range(20)], visual_sources=[]) for phase in ('development', 'climax', 'ending')]
        plan = dict(events_by_part=[events], scene_contract={}, situation_plan_version=4,
                    evidence_capacity_policy='distinct_facts', required_sentence_count=24)
        def generate(request):
            packet, _ = json.JSONDecoder().raw_decode(request.split('\n', 1)[1])
            return dict(duplicates=[], story_spine=[dict(event_id=e['event_id'],
                phase=e['event_id'], summary='Dien bien.', fact_ids=[e['facts'][0]['fact_id']])
                for e in packet['events']])
        result = allocate_sentences(editorial_plan(plan, generate), 24, 480)
        counts = {e['event_id']: e['sentence_quota'] for e in result['events_by_part'][0]}
        self.assertGreater(counts['climax'], counts['development'])
        self.assertEqual(counts['ending'], 1)
        self.assertEqual(sum(counts.values()), 24)

    def test_editorial_batches_bound_large_single_event_without_losing_facts(self):
        from coherent_recap import editorial_batches
        event = dict(event_id='E1', observed_facts=[dict(fact_id=f'F{i}') for i in range(61)])
        batches = list(editorial_batches([event]))
        self.assertEqual([len(b[0]['observed_facts']) for b in batches], [24, 24, 13])
        self.assertEqual([f['fact_id'] for b in batches for e in b for f in e['observed_facts']],
                         [f'F{i}' for i in range(61)])

    def test_editorial_truncated_json_splits_only_failed_fragment(self):
        from coherent_recap import editorial_plan
        from recap_visual_media import parse_writing_response
        facts = [dict(fact_id=f'F{i}', text=f'Dien bien {i}.', shot_ids=[f'S{i}']) for i in range(6)]
        plan = dict(events_by_part=[[dict(event_id='E1', observed_facts=facts)]], scene_contract={})
        completed = []
        def generate(request):
            packet, _ = json.JSONDecoder().raw_decode(request.split('\n', 1)[1])
            ids = [f['fact_id'] for e in packet['events'] for f in e['facts']]
            if len(ids) > 2:
                return parse_writing_response('{"story_spine":[{"summary":"cut')
            self.assertFalse(set(ids) & set(completed))
            completed.extend(ids)
            return dict(duplicates=[], story_spine=[dict(event_id='E1', fact_ids=ids, summary='Dien bien.')])
        result = editorial_plan(plan, generate)
        self.assertEqual(set(completed), {f'F{i}' for i in range(6)})
        self.assertEqual(len(result['events_by_part'][0][0]['observed_facts']), 6)

    def test_editorial_single_fact_json_failure_has_bounded_retry(self):
        from coherent_recap import editorial_plan
        calls = []
        def generate(request):
            calls.append(request)
            raise ValueError('VISUAL_WRITING_JSON_INVALID: incomplete')
        plan = dict(events_by_part=[[dict(event_id='E1', observed_facts=[
            dict(fact_id='F1', text='Dien bien.')])]], scene_contract={})
        with self.assertRaisesRegex(ValueError, 'VISUAL_WRITING_JSON_INVALID'):
            editorial_plan(plan, generate)
        self.assertEqual(len(calls), 2)

    def test_editorial_omissions_cannot_break_mandatory_sentence_budget(self):
        from coherent_recap import editorial_plan
        from narrative_contract import allocate_sentences
        facts = [dict(fact_id=f'F{i}', text=f'Thong tin rieng {i}.', shot_ids=[f'S{i}'])
                 for i in range(320)]
        plan = dict(required_sentence_count=281, events_by_part=[[
            dict(event_id='E1', observed_facts=facts, visual_sources=[])]],
            scene_contract={}, situation_plan_version=4, evidence_capacity_policy='distinct_facts')
        selected = editorial_plan(plan, lambda _: dict(duplicates=[],
            omit_fact_ids=[f'F{i}' for i in range(102)], story_spine=[
                dict(event_id='E1', fact_ids=['F319'], summary='Ket qua.')]))
        event = selected['events_by_part'][0][0]
        self.assertEqual(len(event['reserve_facts']), 320)
        self.assertEqual(len(event['observed_facts']), 281)
        self.assertEqual(len(event['low_priority_fact_ids']), 102)
        self.assertEqual(selected['editorial_selection']['selected_capacity'], 281)
        self.assertEqual(len(selected['editorial_selection']['restored_fact_ids']), 63)
        allocated = allocate_sentences(selected, 281, 5620)
        self.assertEqual(allocated['events_by_part'][0][0]['sentence_quota'], 281)
        self.assertEqual(len(plan['events_by_part'][0][0]['observed_facts']), 320)

    def test_editorial_allocation_preserves_281_and_weights_story_ideas(self):
        from narrative_contract import allocate_sentences
        events = []
        for i in range(4):
            events.append(dict(event_id=f'E{i}', story_beat_id=f'B{i // 2}',
                observed_facts=[dict(fact_id=f'F{i}_{j}', text=f'Dien bien moi {j}.', shot_ids=[f'S{j}'])
                                for j in range(100)], visual_sources=[],
                sentence_ideas=[dict(importance=3 if i >= 2 else 1)]))
        plan = dict(events_by_part=[events], situation_plan_version=4,
            evidence_capacity_policy='distinct_facts', scene_contract={},
            editorial_story_spine=[dict(summary='Mach phim.')])
        allocated = allocate_sentences(plan, 281, 5620)
        quotas = [e['sentence_quota'] for e in allocated['events_by_part'][0]]
        self.assertEqual(sum(quotas), 281)
        chapters = allocated['sentence_contract']['chapter_budgets']
        self.assertEqual(sum(c['sentence_count'] for c in chapters), 281)
        self.assertGreater(chapters[1]['sentence_count'], chapters[0]['sentence_count'])
        self.assertEqual(allocated['sentence_contract']['target_count'], 281)
        self.assertNotIn('sentence_quota', events[0])

    def test_editorial_shortage_never_reduces_required_count(self):
        from narrative_contract import allocate_sentences
        plan = dict(events_by_part=[[dict(event_id='E1', visual_sources=[],
            observed_facts=[dict(fact_id='F1', text='Mot dien bien.', shot_ids=['S1'])])]],
            situation_plan_version=4, evidence_capacity_policy='distinct_facts',
            scene_contract={}, editorial_story_spine=[dict(summary='Mach phim.')])
        with self.assertRaisesRegex(ValueError, 'SENTENCE_EVIDENCE_SHORTAGE'):
            allocate_sentences(plan, 281, 5620)

    def test_editorial_selection_keeps_evidence_and_removes_optional_scene(self):
        from coherent_recap import editorial_plan
        plan = dict(events_by_part=[[
            dict(event_id='E1', observed_facts=[dict(fact_id='F1', text='Finds a clue.')]),
            dict(event_id='E2', observed_facts=[dict(fact_id='F2', text='Walks around.')])]],
            scene_contract={})
        result = editorial_plan(plan, lambda _: dict(duplicates=[],
            omit_fact_ids=['F1', 'F2', 'INVALID'], story_spine=[
                dict(event_id='E1', fact_ids=['F1'], summary='Finds a clue.'),
                dict(event_id='E2', fact_ids=['INVALID'], summary='Unsupported.')]))
        self.assertEqual([e['event_id'] for e in result['events_by_part'][0]], ['E1'])
        self.assertEqual(result['editorial_selection']['omitted_fact_ids'], ['F2'])
        self.assertEqual(len(plan['events_by_part'][0]), 2)
        self.assertEqual(story_outline(result), result['editorial_story_spine'])

    def test_editorial_capacity_fallback_preserves_source_facts(self):
        from coherent_recap import editorial_plan
        plan = dict(events_by_part=[[dict(event_id='E1', observed_facts=[
            dict(fact_id='F1', text='Finds a clue.')])]], scene_contract={})
        result = editorial_plan(plan, lambda _: {'duplicates': []})
        self.assertEqual(result['events_by_part'][0][0]['observed_facts'],
                         plan['events_by_part'][0][0]['observed_facts'])
        self.assertEqual(result['editorial_story_spine'], [])

    def test_outline_keeps_chapter_reasoning_across_parts(self):
        explanation = 'Một nhóm nghiên cứu kiểm tra nguồn nước sau khi cây trồng suy yếu. ' * 4
        first = dict(event_id='E1', story_beat_id='B1', story_beat_summary=explanation,
                     state_before='Cây suy yếu.', scene_context={'goal': 'Tìm nguyên nhân.'})
        second = dict(event_id='E2', story_beat_id='B1', outcome='Phát hiện nước nhiễm mặn.')
        third = dict(event_id='E3', story_beat_id='B2', story_beat_summary='Thử nguồn nước mới.')
        outline = story_outline({'events_by_part': [[first], [second, third]]})
        self.assertEqual(len(outline), 2)
        self.assertEqual(outline[0]['summary'], explanation)
        self.assertEqual(outline[0]['goal'], 'Tìm nguyên nhân.')
        self.assertEqual(outline[0]['outcome'], 'Phát hiện nước nhiễm mặn.')
        self.assertEqual(outline[0]['last_event_id'], 'E2')

    def test_outline_uses_director_story_spine_when_event_fields_are_empty(self):
        event = dict(event_id='E1', story_beat_id='B1',
            story_beat_summary='Cả nhóm bỏ nhà đi.',
            story_beat_cause='Không tìm được nơi ở hiện tại.',
            story_beat_goal='Tìm vùng đất tự do.',
            story_beat_state_before='Cả nhóm vẫn ở thành phố.',
            story_beat_outcome='Họ quyết định về thời tiền sử.',
            story_beat_closing=True)
        chapter = story_outline({'events_by_part': [[event]]})[0]
        self.assertEqual(chapter['cause'], 'Không tìm được nơi ở hiện tại.')
        self.assertEqual(chapter['goal'], 'Tìm vùng đất tự do.')
        self.assertEqual(chapter['outcome'], 'Họ quyết định về thời tiền sử.')

    def event(self):
        return test_narration_units.NarrationUnitTests().plan()['events_by_part'][0][0]

    def test_story_batches_preserve_every_locked_unit(self):
        import copy
        from coherent_recap import story_batches
        first = self.event()
        second = copy.deepcopy(first)
        second['event_id'] = 'E_SECOND'
        for unit in second['sentence_slots']:
            unit['unit_id'] += '_SECOND'
        events = [first, second]
        groups = list(story_batches(events, {}, max_sentences=1))
        self.assertEqual([e for group in groups for e in group], events)
        self.assertEqual(len(groups), 2)

    def test_fast_writer_repairs_as_a_group_with_two_calls(self):
        from unittest.mock import patch
        from evidence_writer import write_batch
        events = [self.event(), dict(self.event(), event_id='E_SECOND')]
        calls = []
        def generate(group, prompt):
            calls.append([e['event_id'] for e in group])
            return '{"scenes":[]}'
        with patch('evidence_writer.bind_planned_scenes'):
            with self.assertRaises(ValueError):
                write_batch(events, {}, generate, fast_mode=True,
                            prompt_builder=lambda *args: 'x' * 20000,
                            response_adapter=lambda payload, group: payload)
        self.assertEqual(calls, [[e['event_id'] for e in events]] * 2)

    def test_missing_scene_restores_locked_quota(self):
        scene = recover_events([self.event()])[0]
        scene['scene_id'] = 'S0001'
        locked = {'parts': [{'scenes': [scene]}]}
        restored, ids = restore_locked_structure({'parts': [{'scenes': []}]}, locked)
        self.assertEqual(restored, locked)
        self.assertEqual(ids, ['S0001'])

    def test_missing_or_excess_output_keeps_locked_sentence(self):
        event = self.event()
        uid = event['sentence_slots'][0]['unit_id']
        for raw in [{}, {'extra': 'Bỏ.'}, {uid: 'Cậu mở tủ. Cậu chạy ra ngoài!'}]:
            result = json.loads(decode(json.dumps(raw), [event]))['sentences']
            self.assertEqual(len(result), event['sentence_quota'])
            self.assertEqual(len(sentences(result[0]['text'])), 1)
            self.assertEqual(result[0]['unit_id'], uid)

    def test_recovery_preserves_sources_quota_and_export(self):
        scene = recover_events([self.event()])[0]
        self.assertEqual(len(sentences(scene['tts'])), 1)
        self.assertEqual(len(scene['sentence_proofs']), 1)
        self.assertTrue(is_source_grounded(scene))
        scene.update(scene_id='S0001', sentence_quota=1)
        payload = apply_local_visual_fallbacks({'parts': [{'part': 1, 'scenes': [scene]}]})
        report = enforce_sentence_visual_contract(payload, 1, 12, allow_visual_fallback=True)
        self.assertTrue(report['passed'])
        self.assertTrue(payload['parts'][0]['scenes'][0]['visual_fallback'])
        self.assertIsNot(payload['parts'][0]['scenes'][0].get('grounding_audit_passed'), True)

    def test_story_context_and_previous_text_are_in_prompt(self):
        event = self.event()
        outline = story_outline({'events_by_part': [[event]]})
        prompt = story_prompt([event], {'whole_story_outline': outline,
            'previous_part_tail': [{'tts': 'Cậu bé đang ở trong nhà.'}]})
        self.assertIn('DAN Y TOAN PHIM', prompt)
        self.assertIn('Cậu bé đang ở trong nhà.', prompt)
        self.assertEqual(one_sentence('Cậu chạy. Cậu dừng lại.'), 'Cậu chạy, Cậu dừng lại.')
