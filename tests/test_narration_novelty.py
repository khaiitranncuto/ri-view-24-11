import copy
import unittest

from narration_novelty import deduplicate_plan, duplicate_sentences, review_plan_novelty
from narrative_contract import allocate_sentences
from narration_units import plan_narration_units
from transition_contract import apply_transition_contracts, confirmed_time_change
from grounded_pipeline import audit_narrations
from sentence_visual_contract import inspect_sentence_visual_contract


class NarrationNoveltyTests(unittest.TestCase):
    def event(self, eid, texts, start=0, situation='S1', repeat=''):
        facts = [dict(fact_id=f'{eid}F{i}', text=t, shot_ids=[f'SHOT_{start+i}'],
                      canonical_actor_ids=['P1']) for i, t in enumerate(texts)]
        return dict(event_id=eid, scene_id=eid, observed_facts=facts, situation_id=situation,
            repeat_group=repeat, scene_context={'location': 'Hang động', 'time_context': 'Ban ngày'},
            start_seconds=start, end_seconds=start + len(texts),
            visual_sources=[dict(shot_id=f['shot_ids'][0], fact_ids=[f['fact_id']],
                start_seconds=start+i, end_seconds=start+i+1) for i, f in enumerate(facts)])

    def plan(self, events):
        return dict(events_by_part=[events], situation_plan_version=4,
                    evidence_capacity_policy='distinct_facts', scene_contract={})

    def test_duplicate_removed_and_budget_reallocated_to_unused_facts(self):
        a = self.event('E1', ['Kukuru ngủ.'], repeat='sleep')
        b = self.event('E2', ['Kukuru ngủ.'], 1, repeat='sleep')
        c = self.event('E3', ['Kukuru tỉnh dậy.', 'Kukuru lấy giáo.', 'Kukuru ra ngoài.'], 2, 'S2')
        plan = self.plan([a, b, c])
        original = copy.deepcopy(plan)
        cleaned = deduplicate_plan(plan)
        self.assertEqual(plan, original)
        self.assertEqual([e['event_id'] for e in cleaned['events_by_part'][0]], ['E1', 'E3'])
        allocated = allocate_sentences(cleaned, 4, 80)
        self.assertEqual([e['sentence_quota'] for e in allocated['events_by_part'][0]], [1, 3])
        units = plan_narration_units(allocated)
        self.assertEqual(sum(len(e['sentence_slots']) for e in units['events_by_part'][0]), 4)

    def test_partial_repeat_keeps_new_information_and_owned_footage(self):
        a = self.event('E1', ['Nhóm bạn bị trói.'], repeat='prison')
        b = self.event('E2', ['Nhóm bạn bị trói.', 'Kiếm hổ tiến vào.'], 1, repeat='prison')
        cleaned = deduplicate_plan(self.plan([a, b]))['events_by_part'][0][1]
        self.assertEqual([f['text'] for f in cleaned['observed_facts']], ['Kiếm hổ tiến vào.'])
        self.assertEqual(cleaned['visual_sources'][0]['fact_ids'], ['E2F1'])

    def test_same_action_on_separate_footage_is_not_automatically_deleted(self):
        events = [self.event('E1', ['Kukuru nhảy.']), self.event('E2', ['Kukuru nhảy.'], 1)]
        self.assertEqual(len(deduplicate_plan(self.plan(events))['events_by_part'][0]), 2)

    def test_return_to_previous_situation_does_not_erase_recurring_action(self):
        events = [self.event('E1', ['Kukuru ngủ.'], repeat='sleep'),
                  self.event('E2', ['Nobita đi học.'], 1, 'S2'),
                  self.event('E3', ['Kukuru ngủ.'], 2, repeat='sleep')]
        self.assertEqual(len(deduplicate_plan(self.plan(events))['events_by_part'][0]), 3)

    def test_shortage_never_silently_reduces_target_or_pads(self):
        events = [self.event('E1', ['Kukuru ngủ.'], repeat='sleep'),
                  self.event('E2', ['Kukuru ngủ.'], 1, repeat='sleep')]
        with self.assertRaisesRegex(ValueError, 'SENTENCE_EVIDENCE_SHORTAGE'):
            allocate_sentences(deduplicate_plan(self.plan(events)), 2, 40)

    def test_different_time_dimensions_do_not_create_false_jump(self):
        for a, b in [('Thế kỷ 21', 'Ban ngày'), ('Ngày hôm sau', 'Ban ngày'),
                     ('Ban ngày', 'Buổi sáng')]:
            self.assertFalse(confirmed_time_change(a, b))
        self.assertTrue(confirmed_time_change('Ban ngày', 'Đêm'))
        self.assertTrue(confirmed_time_change('Buổi sáng', 'Buổi tối'))

    def test_continuation_across_technical_events_does_not_force_bridge(self):
        a = self.event('E1', ['Kukuru đi.'])
        b = self.event('E2', ['Kukuru chạy.'], 1)
        a['scene_context']['time_context'] = 'Thế kỷ 21'
        plan = apply_transition_contracts(self.plan([a, b]))
        contract = plan['events_by_part'][0][1]['transition_contract']
        self.assertEqual(contract['relation'], 'continuation')
        self.assertFalse(contract['requires_bridge'])

    def test_exact_repeat_is_rejected_even_if_verifier_accepts(self):
        scenes = [dict(scene_id=f'S{i}', tts='Kukuru nhìn thấy khói.',
                       observed_facts=[dict(fact_id='F1', shot_ids=['SHOT_1'])],
                       grounded_plan_locked=True, evidence_sentence_plan_version=1) for i in range(2)]
        def audit(cards):
            return {'checks': [dict(scene_id=c['scene_id'], faithful=True, reason='') for c in cards]}
        result = audit_narrations({'parts': [{'scenes': scenes}]}, audit)
        self.assertTrue(result['parts'][0]['scenes'][0]['grounding_audit_passed'])
        second = result['parts'][0]['scenes'][1]
        self.assertFalse(second['grounding_audit_passed'])
        self.assertIn('duplicate_narration:', second['visual_alignment_errors'][0])

    def test_fallback_cannot_hide_semantic_repeat(self):
        scene = dict(scene_id='S1', tts='Kukuru ngủ.', observed_facts=[],
            visual_fallback=True, visual_alignment_errors=['unsupported_narration:duplicate_narration:S0'])
        report = inspect_sentence_visual_contract({'parts': [{'scenes': [scene]}]}, 1,
                                                  allow_visual_fallback=True)
        self.assertIn('DUPLICATE_NARRATION', [i['code'] for i in report['issues']])

    def test_fallback_report_cannot_hide_original_content_failures(self):
        for reason, code in [('duplicate_narration:S0', 'DUPLICATE_NARRATION'),
                             ('transition_state_conflict:still fishing', 'NARRATION_FLOW_CONFLICT')]:
            scene = dict(scene_id='S1', tts='Kukuru ngủ.', observed_facts=[],
                visual_fallback={'reasons': ['unsupported_narration:' + reason]}, visual_alignment_errors=[])
            report = inspect_sentence_visual_contract({'parts': [{'scenes': [scene]}]}, 1,
                                                      allow_visual_fallback=True)
            self.assertIn(code, [i['code'] for i in report['issues']])

    def test_recent_prose_crosses_parts_and_changes_invalidate_cache(self):
        scenes = [dict(scene_id=f'S{i}', tts=f'Nhân vật làm việc {i}.', observed_facts=[],
                       grounded_plan_locked=True, evidence_sentence_plan_version=1) for i in range(4)]
        seen, cache = [], {}
        def audit(cards):
            seen.extend(cards)
            return {'checks': [dict(scene_id=c['scene_id'], faithful=True, reason='') for c in cards]}
        payload = {'parts': [{'scenes': scenes[:2]}, {'scenes': scenes[2:]}]}
        audit_narrations(payload, audit, save=lambda c: cache.update(c))
        self.assertEqual([r['scene_id'] for r in seen[-1]['recent_narration']], ['S0', 'S1', 'S2'])
        seen.clear()
        scenes[0]['tts'] = 'Nhân vật hoàn thành công việc.'
        audit_narrations(payload, audit, checkpoint=cache)
        self.assertIn('S3', [r['scene_id'] for r in seen])

    def test_semantic_review_removes_paraphrase_before_reallocating_count(self):
        a = self.event('E1', ['Nhóm bạn bị trói treo.'])
        b = self.event('E2', ['Các bạn đang bị treo và trói.', 'Kiếm hổ tiến vào.'], 1)
        def generate(prompt):
            return {'duplicates': [dict(fact_id='E2F0', retained_fact_id='E1F0',
                same_occurrence=True, adds_information=False)]}
        cleaned = review_plan_novelty(self.plan([a, b]), generate)
        allocated = allocate_sentences(cleaned, 2, 40)
        self.assertEqual([e['sentence_quota'] for e in allocated['events_by_part'][0]], [1, 1])
        self.assertEqual(allocated['events_by_part'][0][1]['observed_facts'][0]['text'], 'Kiếm hổ tiến vào.')

    def test_semantic_review_preserves_partial_new_information_and_other_actors(self):
        a = self.event('E1', ['Kukuru ngồi trong hang.'])
        b = self.event('E2', ['Kukuru ngồi trong hang và lấy còi.'], 1)
        def generate(prompt):
            return {'duplicates': [dict(fact_id='E2F0', retained_fact_id='E1F0',
                same_occurrence=True, adds_information=True)]}
        self.assertEqual(len(review_plan_novelty(self.plan([a, b]), generate)['events_by_part'][0]), 2)
        b['observed_facts'][0]['canonical_actor_ids'] = ['P2']
        def wrong_actor(prompt):
            return {'duplicates': [dict(fact_id='E2F0', retained_fact_id='E1F0',
                same_occurrence=True, adds_information=False)]}
        self.assertEqual(len(review_plan_novelty(self.plan([a, b]), wrong_actor)['events_by_part'][0]), 2)

    def test_returning_actor_retains_last_source_state(self):
        a = self.event('E1', ['Kukuru lên bờ.'])
        a.update(characters=['P1'], state_after='Đã bắt cá xong.')
        b = self.event('E2', ['Nobita ngủ.'], 1, 'S2')
        b['characters'] = ['P2']
        c = self.event('E3', ['Kukuru thấy khói.'], 2, 'S3')
        c['characters'] = ['P1']
        plan = apply_transition_contracts(self.plan([a, b, c]))
        state = plan['events_by_part'][0][2]['transition_contract']['returning_actor_states']['P1']
        self.assertEqual(state['event_id'], 'E1')
        self.assertEqual(state['activity_state'], 'Đã bắt cá xong.')

    def test_semantic_review_only_sends_facts_with_eligible_pairs(self):
        import json
        from unittest.mock import Mock

        events = [self.event('E1', ['The group is trapped.']),
                  self.event('E2', ['They cannot get out.'], 1),
                  self.event('E3', ['Someone closes the door.'], 2),
                  self.event('E4', ['The group is trapped again.'], 90)]
        events[2]['observed_facts'][0]['canonical_actor_ids'] = ['P2']
        generate = Mock(return_value={'duplicates': []})
        review_plan_novelty(self.plan(events), generate)
        generate.assert_called_once()
        request = json.loads(generate.call_args.args[0].split('\n', 1)[1])
        self.assertEqual(request['target_ids'], ['E2F0'])
        self.assertEqual([fact['fact_id'] for fact in request['facts']], ['E1F0', 'E2F0'])

        generate.reset_mock()
        review_plan_novelty(self.plan([events[0], events[2], events[3]]), generate)
        generate.assert_not_called()

    def test_actual_app_review_reuses_content_checkpoint(self):
        import ast
        import json
        import threading
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf8'))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == 'review_narration_facts')
        factory = ast.parse(
            'def factory():\n    pass1_calls = 0\n    novelty_review_unavailable = False\n'
            '    novelty_attempted_ollama = novelty_used_ollama = ollama_unavailable = False\n'
        ).body[0]
        factory.body.extend([node, ast.Return(ast.Name(id=node.name, ctx=ast.Load()))])
        task = {}
        generate = Mock(return_value='{"duplicates": []}')
        env = dict(json=json, copy=copy, task_lock=threading.Lock(), TASKS={'offline': task},
            task_id='offline', selected_model='test', client=None,
            writing_config=SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update)),
            review_duplicates_with_ollama=Mock(return_value={'duplicates': []}),
            generate_gemini_text_with_recovery=generate, record_gemini_usage=None,
            gemini_quota_pacer=None, update_task_fields=lambda task_id, **fields: task.update(fields),
            is_gemini_capacity_503_error=lambda exc: '503 UNAVAILABLE' in str(exc),
            append_task_log=Mock())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                     '<novelty review>', 'exec'), env)
        review = env['factory']()
        self.assertEqual(review('facts version 1'), {'duplicates': []})
        review('facts version 1')
        self.assertEqual(generate.call_count, 1)
        review('facts version 2')
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args.args[3].max_output_tokens, 8192)
        self.assertEqual(generate.call_args.kwargs['capacity_max_retries'], 1)
        self.assertTrue(generate.call_args.kwargs['retry_timeouts'])

        generate.side_effect = RuntimeError('503 UNAVAILABLE')
        self.assertEqual(review('uncached batch'), {'duplicates': []})
        self.assertEqual(review('next batch'), {'duplicates': []})
        review('facts version 1')
        self.assertEqual(generate.call_count, 3)
        env['append_task_log'].assert_called_once()

        review = env['factory']()
        self.assertEqual(review('local batch', duplicate_only=True),
                         {'duplicates': [], '_ollama_review': True})
        self.assertEqual(generate.call_args.kwargs['capacity_max_retries'], 0)
        env['review_duplicates_with_ollama'].assert_called_once()
        self.assertNotIn('omit_fact_ids',
                         env['review_duplicates_with_ollama'].call_args.args[1]['properties'])
        generate.side_effect = None
        self.assertEqual(review('next duplicate batch', duplicate_only=True), {'duplicates': []})
        env['review_duplicates_with_ollama'].assert_called_once()
        env['review_duplicates_with_ollama'].side_effect = OSError('offline')
        generate.side_effect = RuntimeError('503 UNAVAILABLE')
        self.assertEqual(review('offline duplicate batch', duplicate_only=True), {'duplicates': []})
        self.assertEqual(review('another duplicate batch', duplicate_only=True), {'duplicates': []})
        self.assertEqual(env['review_duplicates_with_ollama'].call_count, 2)

        generate.side_effect = RuntimeError('401 UNAUTHENTICATED')
        with self.assertRaisesRegex(RuntimeError, '401'):
            env['factory']()('uncached batch')
        generate.side_effect = None
        env['factory']()('uncached batch')
        self.assertEqual(generate.call_count, 9)


if __name__ == '__main__':
    unittest.main()
