import copy
import unittest

from evidence_writer import text_audit_prompt
from grounded_pipeline import audit_narrations
from transition_contract import apply_transition_contracts


class TransitionContractTests(unittest.TestCase):
    def test_era_change_takes_priority_when_location_also_changes(self):
        _, current = self.contracts(
            self.event('E1', scene_context={'location': 'Đồng cỏ', 'time_context': 'Thời tiền sử'}),
            self.event('E2', temporal_relation='continuation',
                       scene_context={'location': 'Nhà Nobita', 'time_context': 'Hiện tại'}))
        self.assertEqual(current['transition_contract']['relation'], 'time_jump')

    def test_boundary_verifier_receives_previous_scene_across_parts(self):
        first = dict(scene_id='S1', tts='Kukuru bắt được cá.',
                     observed_facts=[{'text': 'Kukuru bắt cá.'}], grounded_plan_locked=True)
        second = dict(scene_id='S2', tts='Tại khu rừng, Kukuru thấy khói.',
                      observed_facts=[{'text': 'Kukuru thấy khói.'}], grounded_plan_locked=True)
        seen, cache = [], {}

        def audit(cards):
            seen.extend(copy.deepcopy(cards))
            return {'checks': [{'scene_id': c['scene_id'], 'faithful': True, 'reason': ''}
                               for c in cards]}

        payload = {'parts': [{'scenes': [first]}, {'scenes': [second]}]}
        audit_narrations(payload, audit, save=lambda value: cache.update(value))
        self.assertEqual(seen[1]['previous_scene']['tts'], first['tts'])
        seen.clear()
        payload['parts'][0]['scenes'][0]['tts'] = 'Kukuru lên bờ với con cá.'
        audit_narrations(payload, audit, checkpoint=cache)
        self.assertEqual([c['scene_id'] for c in seen], ['S1', 'S2'])

    def test_boundary_contract_preserves_source_actors_and_end_facts(self):
        _, current = self.contracts(
            self.event('E1', characters=['P2'], observed_facts=[
                {'fact_id': 'F1', 'text': 'Doraemon bị đánh bại.', 'canonical_actor_ids': ['P2']}]),
            self.event('E2', characters=['P1'], observed_facts=[
                {'fact_id': 'F2', 'text': 'Nobita tỉnh dậy giữa tuyết.'}]))
        contract = current['transition_contract']
        self.assertEqual(contract['previous_end_state']['facts'][0]['actor_ids'], ['P2'])
        self.assertEqual(contract['current_start_state']['facts'][0]['fact_id'], 'F2')

    def event(self, event_id, **updates):
        row = dict(event_id=event_id, event='Một sự kiện được quan sát.',
                   sentence_slots=[{'unit_id': event_id + '_U001'}],
                   scene_context={})
        row.update(updates)
        return row

    def contracts(self, first, second):
        plan = apply_transition_contracts({'events_by_part': [[first, second]]})
        return plan['events_by_part'][0]

    def test_same_situation_is_a_continuation_without_forced_connector(self):
        _, current = self.contracts(
            self.event('E1', situation_id='SAME', characters=['P1']),
            self.event('E2', situation_id='SAME', characters=['P1']))
        contract = current['transition_contract']
        self.assertEqual(contract['relation'], 'continuation')
        self.assertFalse(contract['requires_bridge'])
        self.assertIn('transition_contract', current['sentence_slots'][0])

    def test_confirmed_location_change_requires_a_semantic_bridge(self):
        _, current = self.contracts(
            self.event('E1', scene_context={'location': 'nhà bếp'}),
            self.event('E2', temporal_relation='continuation',
                       scene_context={'location': 'sân trường'}))
        contract = current['transition_contract']
        self.assertEqual(contract['relation'], 'location_change')
        self.assertTrue(contract['requires_bridge'])
        self.assertEqual(contract['previous_location'], 'nhà bếp')
        self.assertEqual(contract['current_location'], 'sân trường')

    def test_unknown_relationship_uses_neutral_reintroduction(self):
        _, current = self.contracts(
            self.event('E1', characters=['P1']),
            self.event('E2', characters=['P2']))
        contract = current['transition_contract']
        self.assertEqual(contract['relation'], 'neutral_reintroduction')
        self.assertEqual(contract['wording_policy'], 'neutral_current_anchor_only')

    def test_cross_part_boundary_uses_archived_observations_and_lookahead(self):
        from coherent_recap import upcoming_story_context

        first = self.event('E1', visual_sources=[{'shot_id': 'SH1'}])
        second = self.event('E2', visual_sources=[{'shot_id': 'SH2'}])
        archive = {
            'shots': [{'shot_id': 'SH1'}, {'shot_id': 'SH2'}],
            'observations': [{'events': [{'grounded_facts': [
                {'fact_id': 'F1', 'text': 'The character looks down.', 'shot_ids': ['SH1']},
                {'fact_id': 'F2', 'text': 'Trees shake.', 'shot_ids': ['SH2']},
                {'fact_id': 'F3', 'text': 'The character may have landed.',
                 'shot_ids': ['SH2'], 'status': 'uncertain'},
            ]}]}],
        }
        plan = apply_transition_contracts({'events_by_part': [[first], [second]]}, archive)
        contract = plan['events_by_part'][1][0]['transition_contract']
        nearby = contract['archive_boundary_context']
        self.assertEqual([fact['fact_id'] for fact in nearby['before']], ['F1'])
        self.assertEqual([fact['fact_id'] for fact in nearby['after']], ['F2'])
        self.assertEqual(contract['relation'], 'neutral_reintroduction')
        self.assertEqual(upcoming_story_context(plan)['E1'][0]['event_id'], 'E2')

    def test_explicit_dependency_allows_consequence_not_a_phrase_template(self):
        _, current = self.contracts(
            self.event('E1', outcome='Trận chiến kết thúc.'),
            self.event('E2', depends_on=['E1'], event='Cả nhóm rời đi.'))
        contract = current['transition_contract']
        self.assertEqual(contract['relation'], 'consequence')
        self.assertIn('explicit_event_dependency', contract['evidence_basis'])
        prompt = text_audit_prompt([{'scene_id': 'S2', 'tts': 'Sau trận chiến, cả nhóm rời đi.',
                                     'observed_facts': [], 'transition_contract': contract}])
        self.assertIn('Y NGHIA', prompt)
        self.assertIn('khong chep dung mot cum tu mau', prompt)

    def test_verified_bridge_can_request_varied_preparation_wording(self):
        _, current = self.contracts(
            self.event('E1', outcome='Cả nhóm xác định nơi kẻ địch ẩn náu.'),
            self.event('E2', bridge_from_previous={
                'relation': 'preparation', 'facts': ['Doraemon lấy bảo bối.']
            }, event='Cả nhóm chuẩn bị bảo bối.'))
        contract = current['transition_contract']
        self.assertEqual(contract['relation'], 'preparation')
        self.assertEqual(contract['wording_policy'], 'free_semantic_paraphrase')
        self.assertIn('verified_bridge_from_previous', contract['evidence_basis'])
        self.assertIn('semantic_goal', contract)

    def test_contract_is_part_of_audit_card_and_cache_signature(self):
        scene = dict(scene_id='S1', tts='Nhân vật bước sang căn phòng khác.',
                     observed_facts=[{'text': 'Nhân vật bước đi.'}],
                     grounded_plan_locked=True,
                     transition_contract={'relation': 'location_change'})
        payload = {'parts': [{'scenes': [scene]}]}
        cards_seen, cache = [], {}

        def audit(cards):
            cards_seen.append(copy.deepcopy(cards))
            return {'checks': [{'scene_id': 'S1', 'faithful': True, 'reason': ''}]}

        audit_narrations(payload, audit, checkpoint=cache, save=lambda value: (cache.clear(), cache.update(value)))
        self.assertEqual(cards_seen[0][0]['transition_contract']['relation'], 'location_change')
        changed = copy.deepcopy(payload)
        changed['parts'][0]['scenes'][0]['transition_contract']['relation'] = 'time_jump'
        audit_narrations(changed, audit, checkpoint=cache)
        self.assertEqual(len(cards_seen), 2)


if __name__ == '__main__':
    unittest.main()
