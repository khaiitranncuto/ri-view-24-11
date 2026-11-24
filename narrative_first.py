"""Plan source clips and write continuous narration from the locked draft."""
import copy
import json

VERSION = "narrative_first_v1"
LOCKED_VERSION = "narrative_locked_visuals_v2"
MATCH_AFTER_WRITING_VERSION = "narrative_match_after_writing_v2"


def deferred_event_hint(event):
    """Describe a locked but not-yet-written event for its successor."""
    return dict(
        event_id=event.get('event_id'), pending_text=True,
        planned_units=[dict(unit_id=unit.get('unit_id'),
                            task=copy.deepcopy(unit.get('narrative_task') or {}),
                            fact_ids=list(unit.get('fact_ids') or []))
                       for unit in event.get('sentence_slots') or []],
        state_before=(event.get('state_before') or
                      (event.get('scene_context') or {}).get('state_before')),
        state_after=(event.get('state_after') or event.get('outcome') or
                     event.get('result') or
                     (event.get('scene_context') or {}).get('state_after')),
        story_beat=event.get('story_beat_summary', ''))


def completed_boundary_context(events, written_rows, missing_event_ids, width=2):
    """Return real completed neighbours around a deferred event in story order."""
    order = {event.get('event_id'): index for index, event in enumerate(events)}
    missing = sorted(order[eid] for eid in missing_event_ids if eid in order)
    if not missing:
        return [], []
    rows = {row.get('event_id'): row for row in written_rows if isinstance(row, dict)}
    before = [rows[event.get('event_id')] for event in events[:missing[0]]
              if event.get('event_id') in rows][-width:]
    after = [rows[event.get('event_id')] for event in events[missing[-1] + 1:]
             if event.get('event_id') in rows][:width]
    compact = lambda row: {'event_id': row.get('event_id'), 'tts': row.get('tts', '')}
    return [compact(row) for row in before], [compact(row) for row in after]


def fit_selected_shots(start, end, anchors, duration):
    """Fit an ordered set of moments to narration time within its source window."""
    points = sorted({point for point in anchors if start <= point < end})
    points = points or [(start + end) / 2]
    edges = [start, *((a + b) / 2 for a, b in zip(points, points[1:])), end]
    available = [b - a for a, b in zip(edges, edges[1:])]
    remaining = min(duration, end - start)
    lengths = [0.0] * len(points)
    for allocated, i in enumerate(sorted(range(len(points)), key=available.__getitem__)):
        lengths[i] = min(available[i], remaining / (len(points) - allocated))
        remaining -= lengths[i]
    spans = []
    for left, right, point, length in zip(edges, edges[1:], points, lengths):
        clip_start = max(left, min(point - length / 2, right - length))
        if spans and clip_start <= spans[-1][0] + spans[-1][1] + 1e-6:
            previous_start, _ = spans[-1]
            spans[-1] = (previous_start, clip_start + length - previous_start)
        else:
            spans.append((clip_start, length))
    return spans


def event_region_windows(events):
    """Allow END to reach the next observation of the same ongoing situation."""
    windows = {}
    for index, event in enumerate(events):
        units = event['sentence_slots']
        start = min(float(event.get('start_seconds', units[0]['source_start'])), units[0]['source_start'])
        end = max(float(event.get('end_seconds', units[-1]['source_end'])), units[-1]['source_end'])
        if index + 1 < len(events):
            following = events[index + 1]
            boundary = float(following['sentence_slots'][0]['source_start'])
            context = event.get('scene_context') or {}
            next_context = following.get('scene_context') or {}
            continuation = (following.get('transition_contract') or {}).get('relation') == 'continuation'
            same_place = bool(context.get('location')) and context.get('location') == next_context.get('location')
            same_time = context.get('time_context') == next_context.get('time_context')
            if continuation and same_place and same_time:
                end = max(end, boundary)
            end = min(end, boundary)
        windows[event['event_id']] = dict(region_id=event['event_id'], region_start=start, region_end=end)
    return windows


def visual_first_plan(plan):
    """Freeze an ordered source edit before requesting any narration text."""
    from narration_units import validate_narration_unit_order
    from semantic_visuals import visual_catalog
    result = copy.deepcopy(plan)
    timeline, cursor = [], 0.0
    total_words = sum(int(slot.get('target_words') or 1)
                      for g in result['events_by_part'] for e in g
                      for slot in e['sentence_slots'])
    target_seconds = float(result.get('draft_target_seconds') or total_words / 4.5)
    ordered_events = [event for group in result['events_by_part'] for event in group]
    regions = event_region_windows(ordered_events)
    for group in result['events_by_part']:
        for event in group:
            validate_narration_unit_order(event)
            event['narrative_first_version'] = LOCKED_VERSION
            event_clips = []
            visuals = {v['visual_id']: v for v in visual_catalog(event.get('visual_sources'))}
            facts = {f['fact_id']: f for f in event.get('observed_facts') or []}
            for slot in event['sentence_slots']:
                duration = target_seconds * int(slot.get('target_words') or 1) / max(1, total_words)
                clip = dict(event_id=event['event_id'], unit_id=slot['unit_id'],
                    source_start=slot['source_start'], source_end=slot['source_end'],
                    shot_ids=list(slot['shot_ids']), visual_ids=list(slot['visual_ids']),
                    fact_ids=list(slot['fact_ids']), timeline_start=round(cursor, 6),
                    timeline_end=round(cursor + duration, 6))
                selected = [visuals[vid] for vid in slot['visual_ids']]
                anchors = [point for fid in slot['fact_ids'] for point in facts[fid].get('visual_anchor_seconds', [])]
                clip['shots'] = []
                for visual in selected:
                    start, end = visual['start_seconds'], visual['end_seconds']
                    points = sorted({point for point in anchors if start <= point < end})
                    spans = fit_selected_shots(start, end, points, min(2.5 * max(1, len(points)), duration / len(selected)))
                    clip['shots'].append(dict(start=start, end=end, anchors=points,
                                              **regions[event['event_id']],
                                              spans=[[a, a + length] for a, length in spans]))
                timeline.append(clip)
                event_clips.append(copy.deepcopy(clip))
                cursor += duration
            event['visual_draft'] = event_clips
    expected = int(result['sentence_contract']['target_count'])
    if len(timeline) != expected:
        raise ValueError('VISUAL_DRAFT_SENTENCE_COUNT_MISMATCH')
    result['visual_draft'] = dict(version=2, mode='visual_first',
        timing='estimated_until_tts', duration=round(cursor, 6), clips=timeline)
    return result


def facts_with_visuals(event):
    from evidence_writer import supported_facts
    from narration_units import _unit_visuals
    from source_grounded_writer import visual_catalog

    facts = {fact['fact_id']: fact for fact in supported_facts(event)}
    visuals = visual_catalog(event.get('visual_sources') or [])
    return {fid: fact for fid, fact in facts.items() if _unit_visuals([fid], facts, visuals)}


def validate_visual_evidence(events):
    for event in events:
        facts = facts_with_visuals(event)
        for slot in event['sentence_slots']:
            if not set(slot['fact_ids']) <= facts.keys():
                raise ValueError('WRITER_PLAN_VISUAL_INVALID:' + slot['unit_id'])


def prompt(events, context, previous=None, error=None):
    from coherent_recap import NARRATIVE_RULE
    from evidence_writer import VIETNAMESE_WRITER_RULE, supported_facts
    cards = [dict(event_id=e['event_id'], quota=e['sentence_quota'],
        unit_ids=[u['unit_id'] for u in e['sentence_slots']],
        visual_draft=e.get('visual_draft', []),
        sentence_tasks=[dict(unit_id=u['unit_id'], **u['narrative_task'])
                        for u in e['sentence_slots'] if u.get('narrative_task')],
        **({'locked_facts': {u['unit_id']: u['fact_ids'] for u in e['sentence_slots']}}
           if e.get('narrative_first_version') == LOCKED_VERSION else {}),
        facts=[{k: f[k] for k in ('fact_id', 'text', 'evidence_kind', 'spoken_text')
                if k in f} for f in (facts_with_visuals(e).values()
                    if e.get('narrative_first_version') == MATCH_AFTER_WRITING_VERSION
                    else supported_facts(e))],
        source_order=[dict(visual_id=v.get('visual_id'), shot_ids=v.get('shot_ids') or [v.get('shot_id')],
            fact_ids=v.get('fact_ids', []), start_seconds=v.get('start_seconds'),
            end_seconds=v.get('end_seconds')) for v in e.get('visual_sources') or []],
        identity=e.get('identity_contract', {}),
        purpose=dict(
            goal=(e.get('goal') or (e.get('scene_context') or {}).get('goal')
                  or e.get('story_beat_goal')),
            cause=(e.get('cause') or (e.get('story_beat_cause')
                   if e.get('story_beat_opening') else '')),
            before=(e.get('state_before') or (e.get('story_beat_state_before')
                    if e.get('story_beat_opening') else '')),
            outcome=(e.get('outcome') or e.get('state_after') or e.get('result')
                     or (e.get('story_beat_outcome') if e.get('story_beat_closing') else '')
                     or (e.get('story_beat_state_after') if e.get('story_beat_closing') else ''))),
        story_beat=e.get('story_beat_summary', ''),
        transcript_context_only=' '.join(dict.fromkeys(
            str(value).strip() for value in ([e.get('spoken_text')]
                + [f.get('spoken_text') for f in supported_facts(e)])
            if str(value or '').strip()))[:1200],
        transition=e.get('transition_contract', {}),
        source_region=dict(start=e.get('source_region_start_seconds', e.get('start_seconds')),
                           end=e.get('source_region_end_seconds', e.get('end_seconds'))),
        target_words=e.get('tts_target_words')) for e in events]
    if not error:
        for card in cards:
            card.pop('source_order', None)
    for event, card in zip(events, cards):
        if event.get('narrative_first_version') != LOCKED_VERSION:
            continue
        needed = {fid for unit in event['sentence_slots'] for fid in unit['fact_ids']}
        card['facts'] = [fact for fact in card['facts'] if fact['fact_id'] in needed]
        # Shot coordinates and render timing are owned by the backend.
        card.pop('visual_draft', None)
        card.pop('source_order', None)
        card.pop('transcript_context_only', None)
        card['identity'] = {key: value for key, value in card['identity'].items()
                            if key != 'evidence'}
    outline_fields = ('beat', 'event_id', 'first_event_id', 'last_event_id',
                      'summary', 'before', 'goal', 'cause', 'outcome', 'phase')
    outline = [{key: row[key] for key in outline_fields if row.get(key)}
               for row in context.get('whole_story_outline', [])]
    packet = dict(outline=context.get('whole_story_outline', []),
        before=context.get('previous_part_tail', [])[-3:],
        after=context.get('next_batch_context', [])[:2], events=cards,
        style=context.get('style', ''), requirements=context.get('custom_prompt', ''))
    packet['outline'] = outline
    if error:
        packet['repair'] = dict(error=str(error), previous=previous,
            instructions=(
                'Chi tra cac unit_id duoc giao trong events. Cac cau trong previous la ngu canh '
                'lan can; khong viet lai chung va khong lap thong tin cua chung. '
                'EVIDENCE_INVALID: fact_ids phai la danh sach KHONG RONG, '
                'chi chua fact_id trong facts cua event hien tai. Moi menh de '
                'trong cau phai duoc cac fact nay chung minh. '
                'VISUAL_MISSING: doi chieu source_order cua TAT CA cau trong event. '
                'Chon fact co shot sau hoac cung shot voi cau truoc, khong quay lui '
                'hay dung lai vung da qua. Neu hai cau can cung mot shot thi '
                'gan dung fact cung so huu shot do, khong chon fact ngoai event. '
                'Viet lai cau theo fact hop le da chon, khong chi doi ID de hop thuc hoa loi cu.'))
    locked = events and all(e.get('narrative_first_version') == LOCKED_VERSION for e in events)
    assignment_rule = (
        ' BAN GHEP HINH NHAP trong visual_draft: moi shot nam trong mot vung su kien '
        'region_start/region_end; backend chia lai diem cat theo TTS trong vung do. Doc lien toan bo cum '
        'va outline roi viet loi recap lien mach theo dien bien dang hien. '
        'Moi unit co the gom nhieu shot, diem cat hinh khong bat buoc ket thuc cau. '
        'Thoi luong timeline la uoc tinh de can loi. Backend giu lua chon hinh, '
        'can theo TTS sau khi viet. Ke tu locked_facts; cau noi duoc nhac lai trang thai '
        'da xac nhan cua canh truoc. Transcript giup hieu ngu canh. ' if locked else
        ' Viet loi ke lien mach truoc, sau do danh dau fact_ids ho tro moi cau. '
        ' Duoc chon facts trong cung su kien theo thu tu dien bien. Backend se tim shot '
        ' tu kho hinh theo facts cua cau da viet. ')
    output_rule = ('Tra JSON mapping unit_id -> "Mot cau.".\n' if locked else
        'Moi cau dan fact_ids chung minh TOAN BO noi dung, chi tu su kien cua no. '
        'Khong tron fact dialogue va visual trong mot cau. '
        'Tra JSON mapping unit_id -> {"text":"Mot cau.", "fact_ids":["F1"]}.\n')
    return (VIETNAMESE_WRITER_RULE + NARRATIVE_RULE +
        ' BIEN TAP LOI KE: outline da chon mach su kien va manh moi cho toan phim. '
        'Moi doan theo muc tieu, tro ngai, quyet dinh va ket qua co bang chung. '
        'Luoc dong tac phu; giu manh moi, cam xuc va chi tiet lam doi tinh the. '
        'O cao trao, noi hanh dong quyet dinh va ket qua co bang chung. '
        'Khong ke tat ca facts chi de lap day cau. Khong ket moi doan bang loi tong ket. '
        ' Doc toan bo outline va cac su kien, lap mach ke cho ca chang roi moi tach '
        'thanh cac cau hoan chinh. ' + assignment_rule + 'Moi cau dung mot '
        'unit_id va dung mot cau ngu phap. Giu dung quota va thu tu su kien. '
        'Moi cau phai them thong tin, tranh ke lai cung hanh dong bang tu khac. '
        'sentence_tasks la nhiem vu tung cau da phan bo trong ngan sach bat buoc. '
        'Viet ca doan lien mach, moi cau trien khai mot y moi cua nhiem vu, '
        'khong lap lai y da ke de du quota. Dung dai tu khi ro chu the. '
        'Giu manh moi va loi giai dung vi tri, khong tiet lo ket qua truoc. '
        'Truoc khi tra loi, doc lien cac cau va before/after, sua cho nhay coc '
        'bang facts da cho, kiem tra dung so unit_id va moi unit dung mot cau. '
        'Noi muc dich, tro ngai, chuyen bien va ket qua khi facts xac nhan. '
        'story_beat va outline chi de hieu mach, khong phai bang chung cho unit. '
        'Chi ke y do khi facts hoac loi thoai '
        'trong chinh su kien ho tro; ke gian tiep, khong chep cau thoai va khong doan nguoi noi. '
        'Neu nhieu facts chi mo ta cac dong tac lien tiep cua cung mot viec, hay co dong de '
        'ke gon cac buoc trong facts cua unit. Khong tu them dong co hoac loi bao truoc. '
        'Truoc khi viet moi diem chuyen, doc trang thai cuoi canh truoc, dau canh nay '
        'va bridge_unit_id. Neu co bridge_unit_id, cau mang ID nay la cau noi giua hai cum: '
        'noi ket qua truoc voi hanh dong hien tai theo relation da xac nhan. '
        'Neu quan he nhan qua/thoi gian chua ro, gioi thieu chu the va boi canh moi. '
        'Cau noi dung facts hien tai va trang thai cuoi da xac nhan tu cum truoc. '
        'va returning_actor_states neu tro lai mot tuyen. Dung archive_boundary_context '
        'de hieu hinh va loi thoai hai ben diem cat, chi ke chi tiet do facts cua unit ho tro. '
        'Viet ve noi ngan khi can de nguoi nghe theo kip. Neu chu the va dien bien da ro, '
        'cau ke tu nhien da du; khong can tu noi mau. '
        'Neu quan he thoi gian/nhan qua chua ro, gioi thieu boi canh va chu the; '
        'khong bia cau noi. Ke gian tiep loi thoai, giu dung nhan vat theo identity. '
        'Ket chang noi duoc voi after, dau chang noi duoc voi before. '
        'Giu thu tu dien bien '
        'cua facts. Khi sua, giu cac cau tot va kiem tra hai cho noi. '
        + output_rule
        + json.dumps(packet, ensure_ascii=False, separators=(',', ':')))


def schema(events):
    from writer_contract import slots
    keys = slots(events)
    if events and all(e.get('narrative_first_version') == LOCKED_VERSION for e in events):
        return dict(type='object', properties={key: {'type': 'string'} for key in keys},
                    required=list(keys), additionalProperties=False)
    item = dict(type='object', properties=dict(text=dict(type='string'),
        fact_ids=dict(type='array', items=dict(type='string'))),
        required=['text', 'fact_ids'], additionalProperties=False)
    return dict(type='object', properties={key: item for key in keys},
                required=list(keys), additionalProperties=False)


def ordered_visual_assignment(units, value, facts, visuals, event_id):
    """Backtrack source choices when an early choice blocks a later sentence."""
    from itertools import product
    failed = set()

    def visit(index, previous_start, previous_end, previous_ids):
        if index == len(units):
            return []
        state = (index, previous_start, previous_end, previous_ids)
        if state in failed:
            return None
        uid = units[index]['unit_id']
        if not isinstance(value[uid], dict):
            raise ValueError('NARRATION_UNIT_SCHEMA_INVALID')
        ids = value[uid].get('fact_ids')
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(fid, str) or fid not in facts for fid in ids)):
            raise ValueError('NARRATION_UNIT_EVIDENCE_INVALID:' + uid)
        owners = [_unit_owners(fid) for fid in dict.fromkeys(ids)]
        for selection in product(*owners):
            chosen = sorted({v['visual_id']: v for v in selection}.values(),
                            key=lambda v: (v['start_seconds'], v['end_seconds']))
            start = chosen[0]['start_seconds']
            end = max(v['end_seconds'] for v in chosen)
            visual_ids = frozenset(v['visual_id'] for v in chosen)
            if (start < previous_start - .001
                    or (start < previous_end - .001 and not visual_ids & previous_ids)):
                continue
            remaining = visit(index + 1, start, end, visual_ids)
            if remaining is not None:
                return [chosen] + remaining
        failed.add(state)
        return None

    from narration_units import _unit_visuals
    owner_cache = {}
    def _unit_owners(fid):
        if fid not in owner_cache:
            owner_cache[fid] = _unit_visuals([fid], facts, visuals)
        return owner_cache[fid]

    assignment = visit(0, -1, -1, frozenset())
    return assignment


def bind_response(value, events, *, defer_visuals=False):
    """Validate a whole draft before atomically replacing provisional visual slots."""
    from narrative_contract import sentences
    from narration_units import _unit_visuals, validate_narration_unit_order
    from source_grounded_writer import visual_catalog
    from writer_contract import slots
    expected = slots(events)
    if events and all(e.get('narrative_first_version') == LOCKED_VERSION for e in events):
        if not isinstance(value, dict) or set(value) - expected.keys():
            raise ValueError('NARRATION_UNIT_KEYS_INVALID')
        # Missing or malformed text is repaired per event by unit_response.
        return json.dumps(dict(sentences=[dict(event_id=eid, unit_id=uid, text=value[uid])
            for uid, eid in expected.items() if isinstance(value.get(uid), str)]), ensure_ascii=False)
    if defer_visuals:
        if not isinstance(value, dict) or set(value) - expected.keys():
            raise ValueError('NARRATION_UNIT_KEYS_INVALID')
        return json.dumps({'sentences': [
            dict(event_id=eid, unit_id=uid, text=value[uid].get('text'),
                 fact_ids=value[uid].get('fact_ids'))
            for uid, eid in expected.items() if isinstance(value.get(uid), dict)
        ]}, ensure_ascii=False)
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('NARRATION_UNIT_KEYS_INVALID: return every required ID')
    from evidence_writer import supported_facts
    staged, output = [], []
    for event in events:
        facts = {f['fact_id']: f for f in supported_facts(event)}
        visuals = visual_catalog(event['visual_sources'])
        assigned = copy.deepcopy(event)
        assignments = ordered_visual_assignment(assigned['sentence_slots'], value,
                                                facts, visuals, event['event_id'])
        previous_start, previous_end, previous_visuals = -1, -1, set()
        for unit_index, unit in enumerate(assigned['sentence_slots']):
            row = value[unit['unit_id']]
            if not isinstance(row, dict):
                raise ValueError('NARRATION_UNIT_SCHEMA_INVALID')
            text, ids = row.get('text'), row.get('fact_ids')
            if not isinstance(text, str) or len(sentences(text)) != 1:
                raise ValueError('NARRATION_UNIT_ONE_SENTENCE_REQUIRED:' + unit['unit_id'])
            if (not isinstance(ids, list) or not ids
                    or any(not isinstance(fid, str) or fid not in facts for fid in ids)):
                raise ValueError('NARRATION_UNIT_EVIDENCE_INVALID:' + unit['unit_id']
                    + '; received=' + repr(ids) + '; allowed=' + repr(list(facts)))
            if len({facts[fid].get('evidence_kind', 'visual') for fid in ids}) != 1:
                raise ValueError('NARRATION_UNIT_MIXED_SOURCE:' + unit['unit_id'])
            chosen_by_id = {}
            for fid in ([] if assignments is not None else dict.fromkeys(ids)):
                owners = _unit_visuals([fid], facts, visuals)
                matching = next((v for v in owners if v['visual_id'] in chosen_by_id), None)
                if matching is None:
                    matching = next((v for v in owners if v['start_seconds'] >= previous_start
                                     and (v['start_seconds'] >= previous_end
                                          or v['visual_id'] in previous_visuals)), None)
                if matching is None:
                    raise ValueError(f'NARRATION_UNIT_VISUAL_MISSING:{unit["unit_id"]}:{fid}; '
                                     'rewrite using facts with shots in source order')
                chosen_by_id[matching['visual_id']] = matching
            if assignments is not None:
                chosen_by_id = {v['visual_id']: v for v in assignments[unit_index]}
            chosen = sorted(chosen_by_id.values(), key=lambda v: (v['start_seconds'], v['end_seconds']))
            unit.update(fact_ids=list(dict.fromkeys(ids)),
                visual_id=chosen[0]['visual_id'],
                evidence_kind=facts[ids[0]].get('evidence_kind', 'visual'),
                visual_ids=[v['visual_id'] for v in chosen],
                shot_ids=list(dict.fromkeys(s for v in chosen for s in v['shot_ids'])),
                source_start=min(v['start_seconds'] for v in chosen),
                source_end=max(v['end_seconds'] for v in chosen),
                focus=' '.join(facts[fid]['text'] for fid in ids))
            previous_start = unit['source_start']
            previous_end = unit['source_end']
            previous_visuals = set(chosen_by_id)
            output.append(dict(event_id=event['event_id'], unit_id=unit['unit_id'], text=text.strip()))
        validate_narration_unit_order(assigned)
        staged.append(assigned)
    for event, assigned in zip(events, staged):
        event['sentence_slots'] = assigned['sentence_slots']
    return json.dumps(dict(sentences=output), ensure_ascii=False)


def shot_archive(plan, chunks, source, cache_dir, ffmpeg):
    """Persist every indexed source interval, thumbnail and unfiltered observation."""
    import hashlib
    import os
    import subprocess
    from pathlib import Path
    stat = os.stat(source)
    identity = [os.path.realpath(source), stat.st_size, stat.st_mtime_ns]
    root = Path(cache_dir) / ("shots_" + hashlib.sha256(
        json.dumps(identity).encode()).hexdigest()[:20])
    root.mkdir(parents=True, exist_ok=True)
    shots = plan.get('source_shot_inventory') or []
    # The current source inventory is a fixed six-second grid. Other inventories
    # retain source references and can be decoded directly at their own times.
    grid = bool(shots) and all(
        abs(float(row['start_seconds']) - index * 6) < .01
        and 0 < float(row['end_seconds']) - float(row['start_seconds']) <= 6.01
        for index, row in enumerate(shots))
    marker = root / 'thumbnails.complete'
    if grid and not marker.exists():
        result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error',
            '-y', '-i', str(source), '-an', '-vf', 'fps=1/6,scale=320:-2',
            '-q:v', '5', str(root / '%06d.jpg')],
            capture_output=True, text=True, errors='replace', timeout=1800)
        if result.returncode:
            raise RuntimeError('SHOT_ARCHIVE_FAILED:' + result.stderr[-800:])
        # Missing final intervals are extracted individually below.
    entries = []
    for index, row in enumerate(shots, 1):
        thumbnail = root / f'{index:06d}.jpg'
        if not thumbnail.is_file():
            sample_time = float(row['start_seconds'])
            result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error',
                '-y', '-ss', str(sample_time), '-i', str(source), '-frames:v', '1',
                '-vf', 'scale=320:-2', '-q:v', '5', str(thumbnail)],
                capture_output=True, text=True, errors='replace', timeout=60)
            if result.returncode or not thumbnail.is_file():
                raise RuntimeError('SHOT_THUMBNAIL_FAILED:' + str(row.get('shot_id')))
        entries.append(dict(row, source_path=str(source), thumbnail_path=str(thumbnail)))
    marker.write_text('complete', encoding='utf8')
    archive = dict(version=1, source_identity=identity, shots=entries,
                   observations=chunks)
    path = root / 'inventory.json'
    temporary = root / 'inventory.tmp.json'
    temporary.write_text(json.dumps(archive, ensure_ascii=False), encoding='utf8')
    os.replace(temporary, path)
    return dict(path=str(path), shot_count=len(entries),
                thumbnail_count=sum(bool(r['thumbnail_path']) for r in entries))
