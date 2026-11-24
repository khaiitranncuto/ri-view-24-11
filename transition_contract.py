"""Evidence-backed semantic contracts for narration boundaries.

The backend decides what relationship is safe to express.  The writer remains
free to choose natural Vietnamese wording; no connector phrase is whitelisted.
"""
import copy
import re
import hashlib
import json


VERSION = 3
AUDIT_VERSION = 4


def narration_boundary(previous):
    if previous is None:
        return None
    sid = previous.get('scene_id') or re.sub(r'^E(?=\d+$)', 'S', str(previous.get('event_id') or ''))
    return {'scene_id': sid, 'tts': previous.get('tts', ''),
            'observed_facts': copy.deepcopy(previous.get('observed_facts') or [])}


def boundary_audit_signature(signature, boundary):
    return hashlib.sha256(json.dumps([AUDIT_VERSION, signature, boundary],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
_EXPLICIT = {
    'continuation': 'continuation',
    'time_jump': 'time_jump',
    'flashback': 'time_jump',
    'flashforward': 'time_jump',
    'parallel': 'parallel',
    'return': 'return',
    'cause': 'consequence',
    'consequence': 'consequence',
    'preparation': 'preparation',
    'completion': 'completion',
    'discovery_reaction': 'discovery_reaction',
    'obstacle_solution': 'obstacle_solution',
    'location_change': 'location_change',
}
_GENERIC_CONTEXT = {'', 'unknown', 'khong ro', 'không rõ', 'none', 'n/a'}


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _known(value):
    value = _text(value)
    return value if value.casefold() not in _GENERIC_CONTEXT else ''


def time_dimensions(value):
    """Compare like time dimensions; an era and daylight can coexist."""
    text = _known(value).casefold()
    result = {}
    for key, pattern in (
        ('era', r'thế kỷ\s*\d+|tiền sử|hiện đại|hiện tại|cổ đại'),
        ('day', r'ngày hôm sau|hôm sau|hôm trước|cùng ngày'),
        ('daylight', r'ban ngày|ban đêm|đêm'),
        ('dayperiod', r'buổi sáng|buổi trưa|buổi chiều|buổi tối|hoàng hôn|bình minh')):
        match = re.search(pattern, text)
        if match:
            result[key] = match.group().replace('ngày hôm sau', 'hôm sau').replace('ban đêm', 'đêm').replace('hiện tại', 'hiện đại')
    return result


def confirmed_time_change(left, right):
    a, b = time_dimensions(left), time_dimensions(right)
    return any(a[key] != b[key] for key in a.keys() & b.keys())


def _characters(event):
    values = event.get('characters') or event.get('character_ids') or []
    if isinstance(values, str):
        values = [values]
    return sorted({_text(value) for value in values if _text(value)})


def _anchor(event, fields):
    for field in fields:
        value = _known(event.get(field))
        if value:
            return value
    return ''


def boundary_state(event, tail=False):
    """Source facts, not guessed travel or chronology, describe each boundary."""
    facts = [dict(fact_id=f.get('fact_id'), text=f.get('text'),
                  actor_ids=f.get('canonical_actor_ids') or f.get('actor_ids') or [])
             for f in event.get('observed_facts') or [] if isinstance(f, dict)]
    return {'event_id': event.get('event_id'),
            'characters': _characters(event),
            'scene_context': copy.deepcopy(event.get('scene_context') or {}),
            'activity_state': _anchor(event, ('state_after', 'outcome', 'result') if tail
                                      else ('state_before', 'cause')),
            'facts': facts[-2:] if tail else facts[:2]}


def _contract(previous, current, first=False):
    current_context = current.get('scene_context') or {}
    if first:
        return {
            'version': VERSION, 'relation': 'opening', 'requires_bridge': False,
            'confidence': 1.0, 'current_event_id': current.get('event_id'),
            'current_anchor': _anchor(current, ('event', 'narration_focus', 'action')),
            'evidence_basis': ['story_opening'],
        }

    previous_context = previous.get('scene_context') or {}
    bridge = current.get('bridge_from_previous') or {}
    bridge_relation = (
        _text(bridge.get('relation')).casefold() if isinstance(bridge, dict) else ''
    )
    bridge_facts = (
        bridge.get('facts') or bridge.get('shot_ids') or []
        if isinstance(bridge, dict) else []
    )
    raw_relation = _text(current.get('temporal_relation')).casefold()
    relation = _EXPLICIT.get(raw_relation)
    basis = []
    confidence = 0.55

    if bridge_relation in _EXPLICIT and bridge_facts:
        relation = _EXPLICIT[bridge_relation]
        basis.append('verified_bridge_from_previous')
        confidence = 0.94

    depends_on = current.get('depends_on') or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if previous.get('event_id') and previous.get('event_id') in depends_on:
        relation = 'consequence'
        basis.append('explicit_event_dependency')
        confidence = 0.92

    if relation and relation != 'consequence' and 'verified_bridge_from_previous' not in basis:
        basis.append('planner_temporal_relation')
        confidence = 0.9 if relation != 'continuation' else 0.75

    previous_location = _known(previous_context.get('location'))
    current_location = _known(current_context.get('location'))
    previous_time = _known(previous_context.get('time_context'))
    current_time = _known(current_context.get('time_context'))
    same_situation = bool(_known(previous.get('situation_id')) and
                          _known(previous.get('situation_id')) == _known(current.get('situation_id')))
    same_beat = bool(_known(previous.get('story_beat_id')) and
                     _known(previous.get('story_beat_id')) == _known(current.get('story_beat_id')))

    # Confirmed planner relations take precedence.  Context comparisons are
    # only used when the planner did not identify a non-continuation relation.
    if relation in (None, 'continuation'):
        if confirmed_time_change(previous_time, current_time):
            relation, confidence = 'time_jump', 0.82
            basis.append('confirmed_time_context_change')
        elif previous_location and current_location and previous_location.casefold() != current_location.casefold():
            relation, confidence = 'location_change', 0.85
            basis.append('confirmed_location_change')
        elif same_situation or same_beat:
            relation, confidence = 'continuation', 0.8
            basis.append('same_situation' if same_situation else 'same_story_beat')
        elif relation != 'continuation':
            relation = 'neutral_reintroduction'
            basis.append('relationship_unconfirmed')

    previous_ids = _characters(previous)
    current_ids = _characters(current)
    if (relation == 'continuation' and previous_ids and current_ids
            and not set(previous_ids) & set(current_ids) and not (same_situation or same_beat)):
        relation, confidence = 'neutral_reintroduction', 0.65
        basis.append('subject_changed_without_confirmed_link')

    if relation is None:
        relation = 'neutral_reintroduction'
        basis.append('relationship_unconfirmed')

    previous_anchor = _anchor(previous, ('outcome', 'state_after', 'result', 'consequence',
                                         'event', 'narration_focus'))
    current_anchor = _anchor(current, ('state_before', 'cause', 'event', 'narration_focus', 'action'))
    semantic_goals = {
        'continuation': 'continue_same_situation_without_repeating_the_previous_fact',
        'consequence': 'connect_verified_previous_outcome_to_current_action',
        'preparation': 'state_verified_preparation_before_the_supported_action',
        'completion': 'mark_verified_action_completion_before_the_next_state',
        'discovery_reaction': 'connect_verified_discovery_to_observed_reaction',
        'obstacle_solution': 'connect_observed_obstacle_to_verified_solution',
        'parallel': 'switch_to_a_confirmed_parallel_story_line',
        'return': 'return_to_a_previously_confirmed_story_line',
        'time_jump': 'signal_a_confirmed_change_in_time',
        'location_change': 'introduce_the_confirmed_new_location',
        'neutral_reintroduction': 'reintroduce_only_the_confirmed_current_subject_or_place',
    }
    return {
        'version': VERSION,
        'relation': relation,
        'requires_bridge': relation not in ('opening', 'continuation'),
        'confidence': confidence,
        'previous_event_id': previous.get('event_id'),
        'current_event_id': current.get('event_id'),
        'previous_anchor': previous_anchor,
        'current_anchor': current_anchor,
        'previous_end_state': boundary_state(previous, tail=True),
        'current_start_state': boundary_state(current),
        'previous_characters': previous_ids,
        'current_characters': current_ids,
        'previous_location': previous_location,
        'current_location': current_location,
        'previous_time_context': previous_time,
        'current_time_context': current_time,
        'evidence_basis': list(dict.fromkeys(basis)),
        'semantic_goal': semantic_goals.get(relation, semantic_goals['neutral_reintroduction']),
        'variation_key': f'{relation}:{current.get("event_id") or "unknown"}',
        'forbidden_inferences': [
            'unverified_causality', 'unverified_intention',
            'unverified_simultaneity', 'unverified_action_completion',
        ],
        'wording_policy': ('free_semantic_paraphrase' if relation != 'neutral_reintroduction'
                           else 'neutral_current_anchor_only'),
    }


FIRST_SENTENCE_BRIDGE_RULE = (
    'CHI NOI KHI DOI DIEN BIEN, THOI GIAN HOAC TUYEN KE; ranh gioi event/batch/ID '
    'khong tu dong la ranh gioi dien bien. Cac cau cung dien bien ke tiep tu nhien, '
    'khong ep nhac lai hanh dong cu hoac chen tu noi. '
    'KIEM TRA RANH GIOI: doi chieu previous_end_state va current_start_state. '
    'Khi doi dien bien, cau dau phai cho nguoi nghe hieu dang theo ai, o dau, tiep viec gi. '
    'Khi doi dien bien: hieu HOAT DONG truoc da xong hay con dang dien ra, '
    'khong chi chep chi tiet cuoi cum truoc. Neu bat ca da ket thuc, co the viet '
    '"Sau khi bat ca xong, Kukuru nhin thay cot khoi tu xa"; khong viet "Dang bat ca" '
    'neu mau thuan trang thai da ket thuc; '
    'khong tu them "tro ve lang" neu khong co fact chung minh. '
    'Doi tuyen khong ro dong thoi: dung "Cau chuyen chuyen sang..." hoac '
    '"O phia..."; khong bien nhan vat cua canh truoc thanh chu the canh sau. '
    'Cung hanh trinh thi tiep mach, khong dung "trong mot dien bien khac". '
    'Thieu su kien bi bat/di chuyen thi khong dung tu noi de che lap cho trong: '
    'chi ke trang thai moi duoc chung minh, bao ro doi tuyen. '
    'CAU RECAP DAU CANH MOI: doc canh truoc va transition_contract, roi viet ngay '
    'cau recap dau tien vua noi mach truoc vua ke hanh dong/bien co moi. '
    'Khong them cau noi rieng, khong them unit, khong tang quota, khong tom tat lai ca cum truoc. '
    'Neu nguon xac nhan bat ca da xong va cau be ve lang, co the viet '
    '"Bat ca xong, cau be tro ve lang va phat hien ..."; chi dung khi tung ve co bang chung. '
    'Doi tuyen thi gioi thieu chu the/noi moi ngay trong cau recap dau. '
    'Khong dung "trong khi do" neu chua xac nhan dong thoi; quan he chua ro thi '
    'gioi thieu boi canh hien tai trung tinh. Ap dung ca ranh gioi batch va phan. '
    'Voi neutral_reintroduction, neu viec moi khong ro lien he voi viec truoc, '
    'mo cau bang chu the, noi chon hoac tinh huong moi da xac nhan. '
    'Khong can giai thich vi sao phim chuyen sang viec moi. '
)


def archive_boundary_context(previous, current, shots, facts_by_shot):
    """Find observed details near both sides of the cut using archived shot IDs."""
    context = {}
    for label, event, tail in (('before', previous, True), ('after', current, False)):
        sources = [shot for source in event.get('visual_sources') or []
                   for shot in (source.get('shot_ids') or [source.get('shot_id')])
                   if shot in shots]
        if not sources:
            continue
        indices = [shots[shot] for shot in sources]
        index = max(indices) if tail else min(indices)
        nearby = (index, index + (-1 if tail else 1))
        facts = {fact['fact_id']: fact for position in nearby
                 for fact in facts_by_shot.get(position, [])}
        context[label] = list(facts.values())[:4]
    return context


def apply_transition_contracts(plan, archive=None):
    """Attach one global, source-ordered boundary contract to every event."""
    result = copy.deepcopy(plan)
    events = [event for group in result.get('events_by_part') or [] for event in group]
    archive = archive or {}
    shots = {row['shot_id']: index for index, row in enumerate(archive.get('shots') or [])}
    facts_by_shot = {}
    for chunk in archive.get('observations') or []:
        for source_event in chunk.get('events') or []:
            for fact in source_event.get('grounded_facts') or []:
                if fact.get('status', 'confirmed') != 'confirmed' or not fact.get('text'):
                    continue
                for shot in fact.get('shot_ids') or []:
                    if shot in shots:
                        facts_by_shot.setdefault(shots[shot], []).append({
                            key: fact[key] for key in
                            ('fact_id', 'text', 'shot_ids', 'evidence_kind', 'spoken_text')
                            if key in fact})
    previous = None
    actor_states = {}
    for event in events:
        contract = _contract(previous, event, first=previous is None)
        if previous and event.get('event_cluster_writer_version') == 2:
            contract['requires_bridge'] = True
            contract['previous_boundary'] = boundary_state(previous, tail=True)
            contract['current_boundary'] = boundary_state(event)
            slots = event.get('sentence_slots') or []
            if slots:
                contract['bridge_unit_id'] = slots[0]['unit_id']
        # A returning storyline can be separated by many unrelated scenes.
        # Keep source states, not an invented narrative recap of the gap.
        contract['returning_actor_states'] = {
            actor: copy.deepcopy(actor_states[actor]) for actor in _characters(event)
            if actor in actor_states and actor_states[actor]['event_id'] != (previous or {}).get('event_id')}
        if previous and contract['relation'] in ('location_change', 'neutral_reintroduction'):
            nearby = archive_boundary_context(previous, event, shots, facts_by_shot)
            if any(nearby.values()):
                contract['archive_boundary_context'] = nearby
        event['transition_contract'] = contract
        event['transition_contract_version'] = VERSION
        slots = event.get('sentence_slots') or []
        for index, slot in enumerate(slots):
            if index == 0:
                slot['transition_contract'] = copy.deepcopy(contract)
            else:
                slot.pop('transition_contract', None)
        previous = event
        for actor in _characters(event):
            actor_states[actor] = boundary_state(event, tail=True)
    result['transition_contract_version'] = VERSION
    return result
