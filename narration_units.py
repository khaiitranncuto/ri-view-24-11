"""Backend-owned narration units: identity/count independent of punctuation."""
import copy
import json
import math

from semantic_visuals import visual_catalog, timecode, seconds


def _source_window(source):
    start = source.get('start_seconds')
    end = source.get('end_seconds')
    if start is None:
        start = seconds(source.get('start'))
    if end is None:
        end = seconds(source.get('end'))
    start, end = float(start), float(end)
    if not math.isfinite(start + end) or not 0 <= start < end:
        raise ValueError('NARRATION_UNIT_SOURCE_WINDOW_INVALID')
    return start, end


def _shot_ids(row):
    return set(row.get('shot_ids') or ([row['shot_id']] if row.get('shot_id') else []))


def _fact_sources(fact_id, fact, sources):
    """Prefer explicit fact ownership; shot overlap is only a legacy fallback."""
    explicit = [source for source in sources if fact_id in (source.get('fact_ids') or [])]
    if explicit:
        return explicit
    fact_shots = set(fact.get('shot_ids') or [])
    return [source for source in sources if fact_shots & _shot_ids(source)]


def _fact_timeline(facts, sources, event_id):
    """Resolve fact order from source shots, independent of model/list order."""
    rows = []
    for original_index, (fact_id, fact) in enumerate(facts.items()):
        fact_shots = set(fact.get('shot_ids') or [])
        owners = _fact_sources(fact_id, fact, sources)
        if not owners:
            raise ValueError(f'NARRATION_UNIT_FACT_SOURCE_MISSING:{event_id}:{fact_id}')
        windows = [_source_window(source) for source in owners]
        rows.append((min(a for a, _ in windows), max(b for _, b in windows),
                     original_index, fact_id, fact_shots))
    return sorted(rows, key=lambda row: (row[0], row[1], row[2], row[3]))


def validate_narration_unit_order(event):
    previous_start, previous_end, previous_visuals = -1.0, -1.0, set()
    for slot in event.get('sentence_slots') or []:
        start = float(slot.get('source_start', -1))
        end = float(slot.get('source_end', -1))
        visuals = set(slot.get('visual_ids') or [])
        shared = bool(previous_visuals & visuals)
        if (not math.isfinite(start + end) or not 0 <= start < end
                or start < previous_start - .001
                or (start < previous_end - .001 and not shared)):
            raise ValueError(f'NARRATION_UNIT_SOURCE_ORDER_INVALID:{event.get("event_id")}:'
                             f'{slot.get("unit_id")}; previous_start={previous_start}; '
                             f'previous_end={previous_end}; start={start}; shared={shared}')
        previous_start, previous_end, previous_visuals = start, end, visuals
    return True


def _unit_visuals(ids, facts, visuals):
    """Return only visuals actually owned by the unit's facts."""
    selected = {}
    for fact_id in ids:
        explicit = [v for v in visuals if fact_id in (v.get('fact_ids') or [])]
        owners = explicit or [v for v in visuals
                              if set(facts[fact_id].get('shot_ids') or []) & _shot_ids(v)]
        for visual in owners:
            selected[visual['visual_id']] = visual
    return sorted(selected.values(), key=lambda v: (v['start_seconds'], v['end_seconds']))


def plan_narration_units(plan):
    from evidence_writer import supported_facts
    result = copy.deepcopy(plan)
    for group in result['events_by_part']:
        for event in group:
            facts = {f['fact_id']: f for f in supported_facts(event)}
            sources = []
            for raw_source in event['visual_sources']:
                start, end = _source_window(raw_source)
                sources.append(dict(raw_source, start_seconds=start, end_seconds=end,
                    start=raw_source.get('start') or timecode(start),
                    end=raw_source.get('end') or timecode(end)))
            visuals = sorted(visual_catalog(sources), key=lambda v: v['start_seconds'])
            if not visuals:
                raise ValueError('NARRATION_UNIT_VISUALS_MISSING:' + event['event_id'])
            slots, quota = [], int(event['sentence_quota'])
            fact_timeline = _fact_timeline(facts, sources, event['event_id'])
            ordered_facts = [row[3] for row in fact_timeline]
            fact_windows = {row[3]: (row[0], row[1], row[4]) for row in fact_timeline}
            distinct_policy = result.get('evidence_capacity_policy') == 'distinct_facts'
            unit_fact_ids = ([ordered_facts[len(ordered_facts) * i // quota:
                               len(ordered_facts) * (i + 1) // quota]
                              for i in range(quota)] if distinct_policy else None)
            unit_starts = ([min(fact_windows[fid][0] for fid in ids)
                            for ids in unit_fact_ids] if distinct_policy else None)
            for i in range(quota):
                a = len(visuals) * i // quota
                b = len(visuals) * (i + 1) // quota
                chosen = visuals[a:max(a + 1, b)]
                ids = list(dict.fromkeys(fid for v in chosen for fid in v['fact_ids'] if fid in facts))
                if distinct_policy:
                    ids = unit_fact_ids[i]
                    start = unit_starts[i]
                    next_start = unit_starts[i + 1] if i + 1 < quota else float('inf')
                    owned = _unit_visuals(ids, facts, visuals)
                    # Allocate the complete event in one forward pass. A fact
                    # may own several shots, but a unit cannot consume a shot
                    # whose start belongs to the following unit. Equal starts
                    # are allowed only for visuals explicitly supporting each
                    # unit; they never open the interval to the rest of event.
                    chosen = [v for v in owned if v['start_seconds'] >= start - .001
                              and (not math.isfinite(next_start)
                                   or v['start_seconds'] < next_start - .001
                                   or (abs(next_start - start) <= .001
                                       and abs(v['start_seconds'] - start) <= .001))]
                if not ids:
                    raise ValueError('NARRATION_UNIT_FACTS_MISSING:' + event['event_id'])
                if not chosen:
                    raise ValueError('NARRATION_UNIT_FACT_VISUAL_MISSING:' + event['event_id']
                                     + ':' + ','.join(ids))
                chosen = sorted(chosen, key=lambda v: (v['start_seconds'], v['end_seconds']))
                source_start = chosen[0]['start_seconds']
                source_end = max(v['end_seconds'] for v in chosen)
                event_word_target = max(quota, int(event.get('tts_target_words') or quota * 20))
                unit_word_target = (
                    event_word_target * (i + 1) // quota
                    - event_word_target * i // quota
                )
                slots.append(dict(unit_id=f'{event["event_id"]}_U{i + 1:03d}',
                    slot_id=f'{event["event_id"]}_C{i + 1:03d}', fact_ids=ids,
                    shot_ids=list(dict.fromkeys(s for v in chosen for s in v['shot_ids'])),
                    visual_ids=[v['visual_id'] for v in chosen],
                    source_start=source_start, source_end=source_end,
                    target_words=unit_word_target,
                    focus=' '.join(facts[fid]['text'] for fid in ids)))
            event.update(sentence_slots=slots, narration_unit_version=1, visual_sources=sources)
            unused_ideas = list(event.get('sentence_ideas') or [])
            for slot in slots:
                matches = [idea for idea in unused_ideas
                           if set(idea['fact_ids']) & set(slot['fact_ids'])]
                if matches:
                    idea = max(matches, key=lambda item: item['importance'])
                    slot['narrative_task'] = dict(role=idea['role'], purpose=idea['purpose'],
                        fact_ids=idea['fact_ids'])
                    unused_ideas.remove(idea)
                else:
                    slot['narrative_task'] = dict(role='development',
                        purpose=slot['focus'], fact_ids=slot['fact_ids'])
            validate_narration_unit_order(event)
    result['narration_unit_version'] = 1
    return result


def unit_schema(events):
    return {'type': 'object', 'properties': {'sentences': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'event_id': {'type': 'string'},
        'unit_id': {'type': 'string'}, 'text': {'type': 'string'}},
        'required': ['event_id', 'unit_id', 'text']}}}, 'required': ['sentences']}


def compact_writer_packet(packet):
    """Losslessly encode repeated values and field names for the text writer."""
    from collections import Counter
    counts = Counter()
    keys = Counter()
    def count(value):
        if isinstance(value, dict):
            for key, child in value.items():
                keys[key] += 1
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)
        elif isinstance(value, str):
            counts[value] += 1
    count(packet)
    aliases = {key: f'k{i}' for i, (key, n) in enumerate(keys.items())
               if n > 2 and len(key) > 8}
    repeated = [value for value, n in counts.items() if n > 1 and len(value) >= 40]
    refs = {value: i for i, value in enumerate(repeated)}
    def encode(value):
        if isinstance(value, dict):
            return {aliases.get(key, key): encode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [encode(child) for child in value]
        if isinstance(value, str) and value in refs:
            return {'$': refs[value]}
        return value
    return {'key_names': {alias: key for key, alias in aliases.items()},
            'shared_text': repeated, 'data': encode(packet)}


def expand_writer_packet(compact):
    """Decode the transport representation for verification and diagnostics."""
    names, texts = compact['key_names'], compact['shared_text']
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {'$'} and isinstance(value['$'], int):
                return texts[value['$']]
            return {names.get(key, key): expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value
    return expand(compact['data'])


def unit_prompt(events, context, previous=None, error=None):
    from evidence_writer import VIETNAMESE_WRITER_RULE, paragraph_card
    from character_identity import render_identity_context
    people = context.get('characters') or []
    cards = []
    for event in events:
        card = paragraph_card(event)
        cards.append(dict(event_id=event['event_id'],
            role=card['narrative_role'], allowed_names=card['allowed_names'],
            identity_contract=card['identity_contract'],
            scene_context=card['scene_context'],
            facts=[dict(f, text=render_identity_context(f['text'], people)) for f in card['facts']],
            units=[dict(unit_id=u['unit_id'], fact_ids=u['fact_ids'],
                        source_start=u.get('source_start'), source_end=u.get('source_end'),
                        target_words=u.get('target_words', 20),
                        **({'transition_contract': u['transition_contract']}
                           if u.get('transition_contract') else {}))
                   for u in event['sentence_slots']],
            **({'transcript_context_only': card['transcript_context_only']}
               if card.get('transcript_context_only') else {})))
    packet = dict(style=context.get('style') or '', requirements=context.get('custom_prompt') or '',
        story=[render_identity_context(str(s)[:600], people)
               for s in context.get('all_part_summaries') or []],
        before=[str(s.get('tts') or '')[-600:]
                for s in (context.get('previous_part_tail') or [])[-3:]],
        after=[render_identity_context(str(s.get('event') or '')[:600], people)
               for s in (context.get('next_batch_context') or [])[:2]], events=cards)
    if error:
        wanted = {e['event_id'] for e in events}
        packet['repair'] = dict(error=str(error), previous=[dict(event_id=r['event_id'],
            **({'units': [dict(unit_id=u.get('unit_id'), text=u.get('text'))
                         for u in r['narration_units']]} if r.get('narration_units') else
               {'text': r.get('paragraph') or r.get('tts') or ''}))
            for r in (previous or {}).get('scenes') or [] if r.get('event_id') in wanted])
    from transition_contract import FIRST_SENTENCE_BRIDGE_RULE
    return (VIETNAMESE_WRITER_RULE + FIRST_SENTENCE_BRIDGE_RULE +
        'VIẾT THEO CỤM TÌNH HUỐNG: đọc story, before và toàn bộ events trước; lập mạch kể cả cụm, '
        'viết như một đoạn liên tục rồi xuất từng câu vào đúng unit_id. ID chỉ để gắn hình, không phải yêu cầu viết câu độc lập. '
        'Giữ nguyên fact, thứ tự, số câu và hình của từng unit; không chuyển chi tiết sang unit khác để làm mượt. '
        'Trong cùng tình huống, kế thừa chủ thể từ câu trước: dùng đại từ hoặc lược chủ ngữ khi rõ nghĩa, '
        'tránh mở liên tiếp bằng cùng tên; khi đổi người hoặc có thể nhầm thì gọi tên lại. '
        'Trước câu đầu mỗi tình huống, đọc transition_contract để biết việc trước đã hoàn thành chưa, '
        'việc mới ở đâu/lúc nào, nối tiếp, chuẩn bị, nhân quả hay đồng thời. requires_bridge=true thì '
        'câu đầu phải thể hiện quan hệ được xác nhận hoặc giới thiệu bối cảnh mới; đưa cầu nối vào câu có sẵn, không thêm unit. '
        'Không suy ra thời gian truyện chỉ từ thứ tự cảnh. “Sau khi…xong” cần bằng chứng hoàn thành; '
        '“trong lúc ấy” cần bằng chứng đồng thời; “để chuẩn bị” cần mục đích đã xác nhận; '
        '“vì vậy/như vậy” cần nhân quả hoặc kết luận có căn cứ. Không cấm “sau đó”, “và” hay bất kỳ từ nối nào '
        'nhưng không chèn máy móc; quan hệ chưa rõ thì giới thiệu nơi/chủ thể hiện tại. '
        'Đọc lại cả cụm: sửa lặp chủ ngữ, đại từ mơ hồ, lặp ý và nhảy bối cảnh; vẫn giữ nguyên từng ID và bằng chứng. '
        'Chỉ nối khi đổi diễn biến/thời gian/tuyến kể; ranh giới ID hoặc cụm kỹ thuật không buộc thêm từ nối. '
        'Hiểu cả hoạt động trước, không chỉ chi tiết cuối: khi nguồn xác nhận đã bắt cá xong, '
        'dùng cách khép hoạt động như “Sau khi bắt cá xong…” nếu phù hợp; không kể thành “Đang bắt cá…”. '
        'Mỗi câu phải bổ sung thông tin khác. Không nhắc lại trạng thái/cảm xúc bằng từ khác để đủ số câu. '
        'MOI ID BAT BUOC DUNG MOT CAU NGU PHAP, hoàn chỉnh, kết thúc bằng dấu câu, đúng thứ tự. '
        'Câu có thể gồm nhiều mệnh đề và shot đã khóa; bám target_words, không viết câu cụt. '
        'Chỉ dùng facts của unit; nếu nhiều unit chung fact, kể các chi tiết có thật khác nhau, không lặp ý. '
        'Giữ nhân vật, hành động, source_order; không bịa kết quả, nội tâm, nhân quả hoặc suy đoán. '
        'identity_contract tách người có mặt với người chỉ được nhắc đến; tên được nhắc trong lời thoại '
        'không chứng minh người đó xuất hiện hay hành động trong hình. Không nhầm "gian khổ" với tên Gian. '
        'Nhân vật phụ/sinh vật dùng tên hoặc mô tả đã khóa; không tự đặt tên riêng, '
        'quan hệ như bố của ai, hay loài sinh vật khi nguồn chưa xác nhận. '
        'scene_context/story/transcript chỉ là ngữ cảnh, không chứng minh hình ảnh; '
        'chỉ kể gián tiếp nội dung fact dialogue xác nhận. '
        'Nối before/after tự nhiên, không kể lại; tên nhất quán, tránh lặp từ nối. '
        'transition_contract khóa quan hệ ngữ nghĩa, không khóa câu chữ: diễn đạt đa dạng nhưng không thêm quan hệ ngoài relation và anchors. '
        'neutral_reintroduction chỉ giới thiệu lại chủ thể/địa điểm đã xác nhận, không ám chỉ trước-sau hay nhân-quả; continuation không bắt buộc có từ nối. '
        'Ending chỉ nêu ngắn kết quả có bằng chứng, không tổng kết. Trả đủ ID/unit. '
        'Chỉ trả JSON {"sentences":[{"event_id":"E0001","unit_id":"E0001_U001",'
        '"text":"Lời kể."}]}.\n'
        + 'Giải mã data trước khi viết: key_names đổi khóa ngắn thành tên đầy đủ; '
          '{"$":n} thay bằng toàn bộ shared_text[n] (từ 0), giữ đủ nghĩa tại mỗi lần tham chiếu. '
          'Đầu ra dùng schema trên.\n'
        + json.dumps(compact_writer_packet(packet), ensure_ascii=False, separators=(',', ':')))



def generate_unit_chunks(event, context, generate, prompt_builder, previous=None, error=None,
                         save_raw=None, max_chars=14000, max_repairs=2):
    """Keep original unit IDs and bind only after the entire event is assembled."""
    from recap_visual_media import parse_writing_response
    from narrative_contract import sentences
    remaining = list(event['sentence_slots'])
    completed = []
    # Reuse only structurally valid, unambiguous units from this event. Semantic
    # audit failures still require a fresh draft of the affected event.
    reusable = {}
    structural_error = str(error or '').startswith(('NARRATION_UNIT_IDS_INVALID',
        'NARRATION_UNIT_ONE_SENTENCE_REQUIRED', 'PARAGRAPH_REPEATED_SENTENCE'))
    if structural_error:
        prior = [u for row in (previous or {}).get('scenes', [])
                 if row.get('event_id') == event['event_id']
                 for u in row.get('narration_units', [])]
        seen_texts = set()
        for slot in remaining:
            matches = [u for u in prior if u.get('unit_id') == slot['unit_id']]
            if not matches or any(u != matches[0] for u in matches):
                continue
            unit = matches[0]
            text = unit.get('text')
            if not isinstance(text, str) or len(sentences(text)) != 1:
                continue
            normalized = ' '.join(text.casefold().split())
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            reusable[slot['unit_id']] = dict(unit, event_id=event['event_id'])
    while remaining:
        if remaining[0]['unit_id'] in reusable:
            completed.append(reusable[remaining.pop(0)['unit_id']])
            continue
        chunk_context = copy.deepcopy(context)
        if completed:
            chunk_context['previous_part_tail'] = (list(context.get('previous_part_tail') or [])
                + [{'tts': ' '.join(u['text'] for u in completed[-3:])}])[-3:]
        selected = []
        for slot in remaining:
            if slot['unit_id'] in reusable:
                break
            candidate = selected + [slot]
            partial = dict(event, sentence_slots=candidate, sentence_quota=len(candidate))
            prompt = prompt_builder([partial], chunk_context, previous, error)
            if selected and len(prompt) > max_chars:
                # Finish this planned sentence only when the overrun is small
                # and at least half its incremental input still fits.
                old = dict(event, sentence_slots=selected, sentence_quota=len(selected))
                old_size = len(prompt_builder([old], chunk_context, previous, error))
                increment = len(prompt) - old_size
                if len(prompt) <= 15500 and max_chars - old_size >= increment / 2:
                    selected = candidate
                break
            selected = candidate
        partial = dict(event, sentence_slots=selected, sentence_quota=len(selected))
        prompt = prompt_builder([partial], chunk_context, previous, error)
        # At this point the event has already been reduced to the smallest safe
        # writing request: one or more complete narration units.  Never discard
        # evidence or stop merely because a single complete unit needs a prompt
        # larger than the batching soft limit; let the model/backend enforce its
        # actual request capacity.
        raw = generate([partial], prompt)
        if save_raw:
            save_raw(raw, [partial])
        payload = parse_writing_response(raw)
        rows = payload.get('sentences') or []
        # Preserve only unambiguous IDs; never attach text by array position.
        valid = {}
        for slot in selected:
            matches = [r for r in rows if isinstance(r, dict)
                       and r.get('unit_id') == slot['unit_id']
                       and r.get('event_id') == event['event_id']
                       and isinstance(r.get('text'), str) and r['text'].strip()]
            if (matches and all(r == matches[0] for r in matches)
                    and len(sentences(matches[0]['text'])) == 1):
                valid[slot['unit_id']] = matches[0]
        for slot in selected:
            if slot['unit_id'] in valid:
                continue
            single = dict(event, sentence_slots=[slot], sentence_quota=1)
            for attempt in range(max_repairs):
                repair = 'NARRATION_UNIT_IDS_INVALID: expected event_id=' + event['event_id'] + '; unit_id=' + slot['unit_id']
                raw = generate([single], prompt_builder([single], chunk_context, previous, repair))
                if save_raw:
                    save_raw(raw, [single])
                try:
                    repaired = parse_writing_response(raw).get('sentences') or []
                except ValueError:
                    continue
                if (len(repaired) == 1 and isinstance(repaired[0], dict)
                        and repaired[0].get('event_id') == event['event_id']
                        and repaired[0].get('unit_id') == slot['unit_id']
                        and isinstance(repaired[0].get('text'), str)
                        and repaired[0]['text'].strip()
                        and len(sentences(repaired[0]['text'])) == 1):
                    valid[slot['unit_id']] = repaired[0]
                    break
        missing = [s['unit_id'] for s in selected if s['unit_id'] not in valid]
        if missing:
            raise ValueError('NARRATION_UNIT_IDS_INVALID:' + event['event_id'] + '; missing=' + str(missing))
        rows = [valid[s['unit_id']] for s in selected]
        completed.extend(rows)
        remaining = remaining[len(selected):]
    return json.dumps({'sentences': completed}, ensure_ascii=False)


def unit_response(payload, events):
    from narrative_contract import sentences, words
    from narrative_first import MATCH_AFTER_WRITING_VERSION
    rows = payload.get('sentences') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('NARRATION_UNIT_SCHEMA_INVALID')
    known = {e['event_id']: e for e in events}
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('event_id') not in known:
            raise ValueError('NARRATION_UNIT_EVENT_INVALID')
        groups.setdefault(row['event_id'], []).append(row)
    result = []
    for eid, event in known.items():
        units = groups.get(eid, [])
        expected = [u['unit_id'] for u in event['sentence_slots']]
        error = None
        if [u.get('unit_id') for u in units] != expected:
            error = 'NARRATION_UNIT_IDS_INVALID:' + eid + '; expected=' + str(expected)
        elif any(not isinstance(u.get('text'), str) or not u['text'].strip() for u in units):
            error = 'NARRATION_UNIT_TEXT_EMPTY:' + eid
        elif any(len(sentences(u['text'])) != 1 for u in units):
            error = 'NARRATION_UNIT_ONE_SENTENCE_REQUIRED:' + eid
        target_words = sum(int(slot.get('target_words') or 20)
                           for slot in event.get('sentence_slots') or [])
        actual_words = sum(words(unit['text']) for unit in units
                           if isinstance(unit.get('text'), str))
        tolerance = max(2, round(target_words * 0.03))
        word_budget_warning = None
        if (not error and
                not target_words - tolerance <= actual_words <= target_words + tolerance):
            # Word budgets guide one targeted rewrite and candidate ranking;
            # they are never a structural reason to discard a valid script.
            word_budget_warning = {
                'target_words': target_words,
                'minimum_words': target_words - tolerance,
                'maximum_words': target_words + tolerance,
                'actual_words': actual_words,
                'distance': max(target_words - tolerance - actual_words,
                                actual_words - target_words - tolerance, 0),
            }
        facts = {f['fact_id']: f for f in event['observed_facts']}
        proofs = []
        deferred = event.get('narrative_first_version') == MATCH_AFTER_WRITING_VERSION
        for index, slot in enumerate(event['sentence_slots']):
            ids = units[index].get('fact_ids') if deferred and index < len(units) else slot['fact_ids']
            if not isinstance(ids, list) or not ids or any(
                    not isinstance(fid, str) or fid not in facts for fid in ids):
                error = 'NARRATION_UNIT_EVIDENCE_INVALID:' + slot['unit_id']
                continue
            kinds = {facts[fid].get('evidence_kind', 'visual') for fid in ids}
            if len(kinds) != 1:
                error = 'NARRATION_UNIT_MIXED_SOURCE:' + slot['unit_id']
            proofs.append(dict(fact_ids=ids, evidence_kind=next(iter(kinds))))
        result.append(dict(event_id=eid, paragraph=' '.join(u.get('text', '') for u in units if isinstance(u.get('text'), str)),
            narration_units=copy.deepcopy(units), sentence_evidence=proofs,
            **({'word_budget_warning': word_budget_warning} if word_budget_warning else {}),
            **({'format_error': error} if error else {})))
    return {'scenes': result}
