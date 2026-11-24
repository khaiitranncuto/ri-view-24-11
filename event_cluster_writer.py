"""Event-sized viewing and narration with post-writing evidence assignment."""
import copy
import json
import math

from narrative_first import MATCH_AFTER_WRITING_VERSION


def source_window(event):
    # Part-local writer cards deliberately omit start_seconds/end_seconds.
    # Media extraction always needs the original full-video coordinates.
    if 'source_region_start_seconds' in event and 'source_region_end_seconds' in event:
        return float(event['source_region_start_seconds']), float(event['source_region_end_seconds'])
    return float(event['start_seconds']), float(event['end_seconds'])


def media_cards(events):
    cards = []
    for event in events:
        start, end = source_window(event)
        sources = []
        cursor = start
        while cursor < end - .05:
            index = int(math.floor(cursor / 6))
            finish = min(end, (index + 1) * 6)
            sources.append(dict(start_seconds=cursor, end_seconds=finish,
                                shot_ids=[f'SHOT_{index + 1:06d}']))
            cursor = finish
        cards.append(dict(scene_id=event['scene_id'], visual_sources=sources))
    return cards


def cluster_plan(plan, max_seconds=180, *, condense_details=False):
    result = copy.deepcopy(plan)
    for group in result['events_by_part']:
        merged = []
        for event in group:
            context = event.get('scene_context') or {}
            previous = merged[-1] if merged else None
            previous_context = (previous or {}).get('scene_context') or {}
            detail = (condense_details and event.get('story_role') == 'development'
                      and event.get('event_kind') in ('reaction', 'preparation')
                      and not event.get('introduces'))
            same = (previous is not None and bool(context.get('location'))
                    and event.get('story_role') != 'ending'
                    and all(context.get(key) == previous_context.get(key)
                            for key in ('location', 'time_context'))
                    and event.get('story_role') == previous.get('story_role')
                    and event.get('story_beat_id') == previous.get('story_beat_id')
                    and event['start_seconds'] >= previous['start_seconds']
                    and 0 <= event['start_seconds'] - previous['end_seconds'] <= (30 if detail else 15)
                    and event['end_seconds'] - previous['start_seconds'] <= max_seconds)
            if not same:
                event['cluster_source_event_ids'] = [event['event_id']]
                merged.append(event)
                continue
            previous['cluster_source_event_ids'].append(event['event_id'])
            previous['end_seconds'] = max(previous['end_seconds'], event['end_seconds'])
            for key in ('observed_facts', 'grounded_facts', 'visual_sources'):
                previous.setdefault(key, []).extend(copy.deepcopy(event.get(key) or []))
            previous['planned_fact_ids'] = list(dict.fromkeys(
                fact['fact_id'] for fact in previous['observed_facts']))
            previous['characters'] = list(dict.fromkeys(
                list(previous.get('characters') or []) + list(event.get('characters') or [])))
            previous['event'] = ' '.join(f['text'] for f in previous['observed_facts'])
            previous['narration_focus'] = previous['event']
            for key in ('state_after', 'result', 'consequence', 'outcome'):
                previous[key] = copy.deepcopy(event.get(key, ''))
        group[:] = merged
    contract = result['scene_contract']
    contract['part_scene_counts'] = [len(group) for group in result['events_by_part']]
    contract['scene_count'] = sum(contract['part_scene_counts'])
    result['event_cluster_writer_version'] = 1
    return result


def prepare_plan(plan, *, lock_evidence=False):
    from narrative_first import visual_first_plan, LOCKED_VERSION
    result = visual_first_plan(plan) if lock_evidence else copy.deepcopy(plan)
    for group in result['events_by_part']:
        for event in group:
            event['narrative_first_version'] = LOCKED_VERSION if lock_evidence else MATCH_AFTER_WRITING_VERSION
            event['event_cluster_writer_version'] = 2
            event['cluster_character_registry'] = copy.deepcopy(result.get('characters') or [])
    return result


def prompt(events, context, previous=None, error=None):
    if all(e.get('event_cluster_writer_version') == 2 for e in events):
        from narrative_first import prompt as write_from_evidence
        local_context = dict(context)
        wanted = {str(e['event_id']) for e in events}
        outline = context.get('whole_story_outline') or []
        local_context['whole_story_outline'] = [
            row for index, row in enumerate(outline)
            if any(outline[near].get('event_id') in wanted
                   for near in range(max(0, index - 1), min(len(outline), index + 2)))
        ]
        return write_from_evidence(events, local_context, previous, error)
    from coherent_recap import NARRATIVE_RULE
    from evidence_writer import VIETNAMESE_WRITER_RULE
    cards = [dict(event_id=e['event_id'], scene_id=e['scene_id'],
        source_start=source_window(e)[0], source_end=source_window(e)[1],
        sentence_count=e['sentence_quota'], unit_ids=[s['unit_id'] for s in e['sentence_slots']],
        facts=e['observed_facts'], transcript=e.get('spoken_text', ''),
        transition=e.get('transition_contract'), identity=e.get('identity_contract')) for e in events]
    packet = dict(events=cards, outline=context.get('whole_story_outline'),
        before=context.get('previous_part_tail'), after=context.get('next_batch_context'),
        style=context.get('style'), requirements=context.get('custom_prompt'))
    packet['characters'] = events[0].get('cluster_character_registry') or []
    if error:
        packet['repair'] = dict(error=str(error), previous=previous)
    return (VIETNAMESE_WRITER_RULE + NARRATIVE_RULE
        + ' XEM TOAN BO CUM SHOT cua su kien trong clip va MANIFEST. Hieu ai dang lam gi, '
        'tro ngai, quyet dinh va ket qua truoc khi viet. Viet mot doan recap lien mach '
        'cho ca su kien, roi moi tach thanh dung sentence_count cau va gan unit_id theo thu tu. '
        'Moi cau duoc chon bat ky fact_ids trong su kien cua no, co the gom nhieu shot. '
        'Khong bi rang buoc voi phan bo fact/hinh nhap tung cau. Khong mo ta moi shot '
        'thanh mot cau. Gop cu chi phu vao dien bien chinh. Chi dung facts da xac nhan '
        'va hinh thuc su trong clip. Neu fact nhan sai nhan vat, tranh ke menh de sai do. '
        'Chon fact_ids chung minh tung cau theo thu tu hinh nguon. '
        'Backend chon hinh SAU khi chot loi ke va can diem cat theo TTS. '
        'Trong cung ket qua, tra _observations=[{event_id,facts:[{fact_id,status:"confirmed",'
        'text,actor_ids}]}] cho TAT CA facts cua su kien, sua nhan vat/h anh dong neu hinh '
        'cho thay fact cu sai. Chi dung ID da giao, actor_ids lay tu characters; khong ro ten '
        'thi mo ta trung tinh. Loi ke phai bam facts vua xac nhan. '
        'Cac khoa con lai la mapping unit_id -> {"text":"Mot cau.","fact_ids":["F1"]}.\n'
        + json.dumps(packet, ensure_ascii=False))


def schema(events):
    from narrative_first import schema as narration_schema
    result = narration_schema(events)
    fact = dict(type='object', properties=dict(fact_id=dict(type='string'),
        status=dict(type='string', enum=['confirmed']), text=dict(type='string'),
        actor_ids=dict(type='array', items=dict(type='string'))),
        required=['fact_id', 'status', 'text', 'actor_ids'], additionalProperties=False)
    result['properties']['_observations'] = dict(type='array', items=dict(type='object',
        properties=dict(event_id=dict(type='string'), facts=dict(type='array', items=fact)),
        required=['event_id', 'facts'], additionalProperties=False))
    result['required'].append('_observations')
    return result


def decode(raw, events):
    from recap_visual_media import parse_writing_response
    from source_fact_repair import reviewed_event
    from narrative_first import bind_response
    value = parse_writing_response(raw)
    if not isinstance(value, dict):
        raise ValueError('EVENT_CLUSTER_RESPONSE_INVALID')
    observations = value.pop('_observations', None)
    if (not isinstance(observations, list) or len(observations) != len(events)
            or any(not isinstance(row, dict) for row in observations)
            or {row.get('event_id') for row in observations} != {event['event_id'] for event in events}):
        raise ValueError('EVENT_CLUSTER_OBSERVATIONS_INVALID')
    lookup = {row['event_id']: row for row in observations}
    staged = [reviewed_event(event, lookup[event['event_id']],
                            event.get('cluster_character_registry') or []) for event in events]
    result = bind_response(value, staged, defer_visuals=True)
    for event, revised in zip(events, staged):
        event.update(revised)
    return result
