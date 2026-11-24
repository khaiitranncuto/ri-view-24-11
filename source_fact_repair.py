"""Bounded source review and coherent situation repair without changing shots."""
import copy
import json
import hashlib


def source_review_key(event, source_identity):
    data = [2, source_identity, event.get('event_id'), event.get('observed_facts'),
            event.get('visual_sources')]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_prompt(event, manifest, characters=None):
    return (
        'Xem HÌNH THỰC TẾ của từng shot trong clip, kiểm tra lại các fact dưới đây. '
        'Không mặc định fact cũ hay nhận xét kiểm chứng là đúng. Phân biệt vào/ra, '
        'đến/đi và chủ thể bằng diễn biến nhìn thấy, không đoán từ cốt truyện. '
        'Mỗi fact_id trả đúng một mục: status=confirmed khi hình đủ rõ, text là '
        'dữ kiện tiếng Việt có dấu đã sửa hoặc giữ nguyên. Nếu không rõ hướng '
        'hành động, chỉ ghi điều quan sát chắc chắn được; nếu không đủ bằng chứng '
        'cho fact thì status=unclear. Không thêm nhân vật, hành động, ID hay shot. '
        'Với mỗi fact, trả actor_ids là ID những nhân vật thực sự thực hiện hành động '
        'trong hình. Người được nhắc trong thoại không phải người có mặt. '
        'Được loại nhân vật nhận nhầm trong fact cũ. Nếu không xác định tên, dùng '
        'mô tả trung tính đúng hình như "nhóm bạn", actor_ids=[]; không đoán danh tính. '
        'Chỉ trả JSON {"facts":[{"fact_id":"F1","status":"confirmed","actor_ids":[], '
        '"text":"Dữ kiện quan sát được."}]}.\nMANIFEST: '
        + json.dumps(manifest, ensure_ascii=False) + '\nFACTS: '
        + json.dumps(event['observed_facts'], ensure_ascii=False)
        + '\nCHARACTERS: ' + json.dumps(characters or [], ensure_ascii=False))


def reviewed_event(event, response, characters=None):
    """Apply independent observations only; retain IDs, quotas and source ranges."""
    from evidence_writer import require_vietnamese_narration
    rows = response.get('facts') if isinstance(response, dict) else None
    facts = event['observed_facts']
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(facts)
            or {r.get('fact_id') for r in rows} != {f['fact_id'] for f in facts}):
        raise ValueError('SOURCE_FACT_REVIEW_IDS_INVALID')
    updates = {r['fact_id']: r for r in rows}
    if characters and any('actor_ids' not in row for row in rows):
        raise ValueError('SOURCE_FACT_REVIEW_ACTORS_MISSING')
    result = copy.deepcopy(event)
    for fact in result['observed_facts']:
        row = updates[fact['fact_id']]
        if row.get('status') != 'confirmed' or not isinstance(row.get('text'), str):
            raise ValueError('SOURCE_FACT_REVIEW_UNRESOLVED:' + fact['fact_id'])
        text = row['text'].strip()
        require_vietnamese_narration(text, fact['fact_id'])
        fact.setdefault('original_observation_text', fact['text'])
        fact['text'] = text
        fact['raw_text'] = text
        fact['source_review_version'] = 1
        if 'actor_ids' in row:
            actors = row['actor_ids']
            known = {c['id'] for c in characters or [] if c.get('id')}
            if (not isinstance(actors, list) or any(not isinstance(cid, str) for cid in actors)
                    or (characters is not None and not set(actors) <= known)):
                raise ValueError('SOURCE_FACT_REVIEW_ACTORS_INVALID:' + fact['fact_id'])
            fact['actor_ids'] = list(dict.fromkeys(actors))
            fact['canonical_actor_ids'] = list(fact['actor_ids'])
    by_id = {f['fact_id']: f['text'] for f in result['observed_facts']}
    result['event'] = ' '.join(by_id.values())
    result['narration_focus'] = result['event']
    corrected = {fact['fact_id']: fact for fact in result['observed_facts']}
    for field in ('reserve_facts', 'grounded_facts'):
        for fact in result.get(field) or []:
            if fact.get('fact_id') in corrected:
                fact.update(copy.deepcopy(corrected[fact['fact_id']]))
    if all('actor_ids' in updates[f['fact_id']] for f in facts):
        result['characters'] = list(dict.fromkeys(cid for f in result['observed_facts'] for cid in f['actor_ids']))
        result['character_refs'] = list(result['characters'])
        from identity_contract import scene_identity_contract
        result['identity_contract'] = scene_identity_contract(result, characters or [])
        result['allowed_character_names'] = sorted(set(result['identity_contract']['visible_names']
                                                       + result['identity_contract']['mentioned_names']))
    for slot in result.get('sentence_slots') or []:
        slot['focus'] = ' '.join(by_id[fid] for fid in slot['fact_ids'])
        slot['narrative_task'] = dict(role='development', purpose=slot['focus'], fact_ids=list(slot['fact_ids']))
    # Descriptions and summaries are context, never proof over reviewed facts.
    for field in ('visual_sources', 'locked_visual_shots'):
        for visual in result.get(field) or []:
            ids = visual.get('fact_ids') or []
            if ids:
                visual['description'] = ' '.join(by_id[fid] for fid in ids if fid in by_id)
    result['source_fact_review_version'] = 1
    return result


def needs_source_review(scene):
    """A text-level transition rejection can originate in a wrong source actor."""
    if scene.get('grounding_audit_pending') or scene.get('grounding_audit_passed'):
        return False
    errors = ' '.join(map(str, scene.get('visual_alignment_errors') or []))
    return scene.get('grounding_audit_method') == 'video' or any(
        reason in errors.casefold() for reason in ('source_actor_conflict:', 'source_action_conflict:',
                                                   'visual_review_required:', 'wrong_actor', 'wrong person',
                                                   'sai nhân vật'))


def refresh_reviewed_sources(payload, events):
    """Old writer candidates must be checked against corrected source evidence."""
    result = copy.deepcopy(payload)
    reviewed = {event['scene_id']: event for event in events
                if event.get('source_fact_review_version') and event.get('scene_id')}
    for part in result.get('parts') or []:
        for scene in part.get('scenes') or []:
            event = reviewed.get(scene.get('scene_id'))
            if event:
                for field in ('observed_facts', 'identity_contract', 'allowed_character_names',
                              'characters', 'transition_contract', 'source_fact_review_version'):
                    if field in event:
                        scene[field] = copy.deepcopy(event[field])
    return result


def coherent_repair_context(scenes, scene_id, context):
    result = copy.deepcopy(context)
    index = next(i for i, s in enumerate(scenes) if s['scene_id'] == scene_id)
    result['previous_part_tail'] = [{'tts': s['tts']} for s in scenes[max(0, index - 2):index]]
    result['next_batch_context'] = [{'event': s['tts']} for s in scenes[index + 1:index + 3]]
    result['custom_prompt'] = str(result.get('custom_prompt') or '') + (
        '\nSỬA CẢ CỤM ĐANG LỖI thành một đoạn recap mạch lạc theo facts vừa '
        'được xem lại. Đoạn trước và sau chỉ là ngữ cảnh, không viết lại chúng. '
        'Nối ý tự nhiên, giữ chủ thể, tên gọi và trình tự nhất quán; chỉ dùng từ '
        'nối khi bằng chứng hỗ trợ, không bịa quan hệ nhân quả. Giữ nguyên '
        'unit_id, số đơn vị, fact_id và shot đã giao. Facts đã xem lại là nguồn '
        'cho hành động; bản cũ và tóm tắt có thể sai.\nCỤM CŨ: '
        + scenes[index]['tts'])
    return result
