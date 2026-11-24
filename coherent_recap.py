"""Kể theo dàn ý toàn phim, giữ quota và phục hồi từ bằng chứng đã khóa."""
import copy
import json
import re


def editorial_batches(events, max_facts=24, max_chars=12000):
    """Bound expected output as well as input, including a large single event."""
    batch, count, size = [], 0, 0
    for event in events:
        facts = event.get('observed_facts') or []
        for offset in range(0, len(facts), max_facts):
            fragment = dict(event, observed_facts=facts[offset:offset + max_facts])
            fragment_size = len(json.dumps(fragment['observed_facts'], ensure_ascii=False))
            if batch and (count + len(fragment['observed_facts']) > max_facts
                          or size + fragment_size > max_chars):
                yield batch
                batch, count, size = [], 0, 0
            batch.append(fragment)
            count += len(fragment['observed_facts'])
            size += fragment_size
    if batch:
        yield batch


def editorial_plan(plan, generate):
    """Select narration material before allocating slots; retain source archives."""
    result = copy.deepcopy(plan)
    events = [e for group in result['events_by_part'] for e in group]
    outline = story_outline(result)
    omitted, spine, ideas = set(), [], {}
    pending = [(batch, False) for batch in editorial_batches(events)]
    while pending:
        batch, retried = pending.pop(0)
        cards = [dict(event_id=e['event_id'], context=e.get('scene_context'),
            facts=[{k: f.get(k) for k in ('fact_id', 'text', 'spoken_text')}
                   for f in e.get('observed_facts') or []]) for e in batch]
        request = (
            'BIEN TAP LOI RECAP TOAN PHIM truoc khi viet. Doc outline va facts. '
            'Chon thong tin de nguoi nghe hieu muc tieu, tro ngai, quyet dinh, '
            'thay doi va ket qua. Giu manh moi duoc giai dap ve sau, chi tiet hai '
            'va cam xuc co tac dung. Chi danh dau uu tien thap cho canh phu; '
            'backend chi chot lua chon sau khi kiem tra du ngan sach cau. '
            'Chi omit_fact_ids cho cu chi, di chuyen, phong canh hoac lap y ma bo di '
            'van hieu truyen. Neu chua chac vai tro thi giu. Khong bo manh moi, '
            'quyet dinh, dao chieu hay ket qua. '
            'Tra duplicates=[], omit_fact_ids=[], story_spine=[{event_id, '
            'fact_ids, summary, phase}]. phase la setup, development, climax hoac ending. '
            'climax la cao trao cuoi, uu tien ke chi tiet hon cao trao giua. '
            'ending CHI la doan sau khi xung dot chinh da giai quyet, tom gon toi da. '
            'Khong co dinh ty le. Nhieu cau thi mo rong manh moi, tro ngai, quyet dinh, '
            'dao chieu o giua va cao trao cuoi; it cau thi gom cac buoc cung dien bien. '
            'Moi summary noi ro quan he giua cac dien bien '
            'duoc facts chung minh, danh dau cho chua ro, khong suy doan dong co. '
            'Tra them sentence_ideas=[{event_id,fact_ids,role,purpose,importance}]. '
            'Moi idea la MOT y moi co the viet thanh mot cau, purpose ghi ro can ke '
            'thong tin gi, khong chi ghi nhan role. role dung setup, goal, obstacle, '
            'decision, action, turning_point, clue, payoff, consequence, emotion, humor. '
            'importance tu 1 den 3 theo tac dung voi mach truyen. Khong tach cu chi '
            'vo nghia de tang so y. Manh moi va loi giai phai ghi ro trong purpose. '
            'Tong so cau bat buoc trong sentence_budget la muc tieu phan bo TOAN PHIM, '
            'khong ep moi batch dat tong do, khong loai y can thiet de lam recap ngan hon. '
            'Chi dung event_id va fact_ids trong batch. Tra JSON gon, khong Markdown. '
            'summary va purpose viet ngan, khong chep lai toan bo facts.\n'
            + json.dumps(dict(outline=outline, previous=spine[-3:], events=cards),
                         ensure_ascii=False))
        request += '\n' + json.dumps(dict(sentence_budget=plan.get('required_sentence_count')))
        if retried:
            request += '\nLan truoc JSON bi cat. Chi tra cac truong can thiet, viet ngan va dong du JSON.'
        try:
            response = generate(request)
            if (not isinstance(response, dict)
                    or any(not isinstance(response.get(key, []), list)
                           for key in ('story_spine', 'sentence_ideas', 'omit_fact_ids'))):
                raise ValueError('EDITORIAL_RESPONSE_SCHEMA_INVALID')
        except ValueError as exc:
            if not str(exc).startswith(('VISUAL_WRITING_JSON_INVALID', 'EDITORIAL_RESPONSE_SCHEMA_INVALID')):
                raise
            if len(batch) > 1:
                middle = len(batch) // 2
                children = [batch[:middle], batch[middle:]]
            else:
                facts = batch[0]['observed_facts']
                if len(facts) > 1:
                    middle = len(facts) // 2
                    children = [[dict(batch[0], observed_facts=part)]
                                for part in (facts[:middle], facts[middle:])]
                elif not retried:
                    children = [batch]
                else:
                    raise
            pending[0:0] = [(child, len(child) == 1 and len(child[0]['observed_facts']) == 1)
                            for child in children]
            continue
        known = {e['event_id']: {f['fact_id'] for f in e.get('observed_facts') or []}
                 for e in batch}
        ids = set().union(*known.values())
        rows = response.get('story_spine') or []
        valid = [row for row in rows if isinstance(row, dict)
                 and row.get('event_id') in known and isinstance(row.get('summary'), str)
                 and isinstance(row.get('fact_ids'), list) and row['fact_ids']
                 and all(isinstance(fid, str) for fid in row['fact_ids'])
                 and set(row['fact_ids']) <= known[row['event_id']]]
        for row in response.get('sentence_ideas') or []:
            if (isinstance(row, dict) and row.get('event_id') in known
                    and isinstance(row.get('purpose'), str) and row['purpose'].strip()
                    and isinstance(row.get('fact_ids'), list) and row['fact_ids']
                    and all(isinstance(fid, str) for fid in row['fact_ids'])
                    and set(row['fact_ids']) <= known[row['event_id']]
                    and row.get('role') in {'setup', 'goal', 'obstacle', 'decision', 'action',
                        'turning_point', 'clue', 'payoff', 'consequence', 'emotion', 'humor'}):
                idea = dict(row, importance=row.get('importance') if row.get('importance') in (1, 2, 3) else 1)
                if idea not in ideas.setdefault(row['event_id'], []):
                    ideas[row['event_id']].append(idea)
        protected = {fid for row in valid for fid in row['fact_ids']}
        protected.update(fid for rows in ideas.values() for row in rows for fid in row['fact_ids'])
        omitted.update(fid for fid in response.get('omit_fact_ids') or []
                       if isinstance(fid, str) and fid in ids and fid not in protected)
        spine.extend(valid)
    from evidence_writer import supported_facts
    required = int(plan.get('required_sentence_count') or 0)
    original_facts = {e['event_id']: copy.deepcopy(e.get('observed_facts') or []) for e in events}
    def capacity():
        return sum(1 if e.get('story_role') == 'ending' and supported_facts(e)
                   else len(supported_facts(e)) for e in events)
    for group in result['events_by_part']:
        for event in group:
            phases = [row.get('phase') for row in spine if row['event_id'] == event['event_id']
                      and row.get('phase') in {'setup', 'development', 'climax', 'ending'}]
            if phases:
                event['story_role'] = phases[-1]
            event['sentence_ideas'] = ideas.get(event['event_id'], [])
            event['reserve_facts'] = copy.deepcopy(original_facts[event['event_id']])
            event['low_priority_fact_ids'] = [f['fact_id'] for f in event['reserve_facts']
                                               if f['fact_id'] in omitted]
            event['observed_facts'] = [f for f in event.get('observed_facts') or []
                                       if f['fact_id'] not in omitted]
    restored = set()
    priority = {'climax': 4, 'development': 2, 'setup': 1, 'ending': 0}
    for event in sorted(events, key=lambda e: priority.get(e.get('story_role'), 2), reverse=True):
        if required and capacity() < required:
            for fact in event['reserve_facts']:
                if fact['fact_id'] not in omitted:
                    continue
                event['observed_facts'].append(copy.deepcopy(fact))
                restored.add(fact['fact_id'])
                if capacity() >= required:
                    break
        selected = {f['fact_id'] for f in event['observed_facts']}
        event['observed_facts'] = [copy.deepcopy(f) for f in event['reserve_facts'] if f['fact_id'] in selected]
    for group in result['events_by_part']:
        for event in group:
            event['planned_fact_ids'] = [f['fact_id'] for f in event['observed_facts']]
        group[:] = [e for e in group if e['observed_facts']]
    if not any(result['events_by_part']):
        raise ValueError('EDITORIAL_PLAN_EMPTY')
    result['editorial_story_spine'] = spine
    result['editorial_selection'] = dict(version=2, omitted_fact_ids=sorted(omitted - restored),
        restored_fact_ids=sorted(restored), reserve_fact_count=sum(map(len, original_facts.values())),
        required_sentence_count=required, selected_capacity=capacity())
    contract = result['scene_contract']
    contract['part_scene_counts'] = [len(g) for g in result['events_by_part']]
    contract['scene_count'] = sum(contract['part_scene_counts'])
    return result

NARRATIVE_RULE = (
    'UU TIEN CAO NHAT: chi lam tu nhien CACH DIEN DAT, moi menh de phai co bang chung. '
    'Duoc dung tu dong nghia giu nguyen y, nhu dung giao san ca va dung giao bat ca. '
    'Khong them noi tam, muc dich, danh gia muc do, loi bao truoc hay ket luan tu outline. '
    'Vi du fact "Kukuru dung giao bat ca" chi ho tro "Kukuru dung giao san ca"; '
    'khong ho tro "cau tan huong cuoc song binh yen truoc khi bien co xay ra". '
    'Chi them "nhanh nhen", "tham te", "tui than", "de thoat khoi su kiem soat" '
    'khi bang chung xac nhan dung y do. Neu facts ngan thi viet cau ngan. '
    'target_words chi la uoc tinh; khong them thong tin hoac lap y de du tu. '
    'BIEN TAP RECAP CHO NGUOI CHUA XEM VIDEO. Truoc khi viet, doc mach toan bai va '
    'xac dinh trong doan nay ai dang theo duoi dieu gi, tro ngai nao xuat hien, '
    'hanh dong nao lam thay doi tinh hinh va ket qua dan toi viec gi. '
    'Voi tai lieu/giai thich, theo cau hoi, bang chung va phat hien cua nguon. '
    'Chi dung muc dich, nguyen nhan, ket qua va cam xuc duoc facts hoac loi thoai xac nhan. '
    'Chon chi tiet giup hieu quyet dinh, nguy hiem, bat ngo, hai huoc hoac ket qua; '
    'luoc cac dong tac dung, ngoi, cam do, nhin va di chuyen khi chung khong them y nghia. '
    'Moi chang phai giu du chuoi: nhan vat muon gi, dieu gi can tro, ho quyet dinh lam gi, '
    'va tinh hinh thay doi ra sao. Neu facts chi la nhieu dong tac cung phuc vu mot muc tieu, '
    'co dong thanh mot dien bien; khong bien moi shot thanh mot cau bao cao. '
    'Uu tien thong tin giai thich vi sao su kien tiep theo xay ra va chi tiet duoc goi lai ve sau. '
    'Gom cac chi tiet phu lien quan vao cung mot cau. Doc ca doan nhu dang ke cho mot nguoi nghe, '
    'roi moi gan tung cau vao ID. Moi cau phai day mach ke hoac giai thich thong tin can thiet. '
    'Khi doi tuyen, dinh vi lai nhan vat va viec dang tiep dien; khi quay lai, ke tiep '
    'trang thai da biet cua tuyen do. Tu noi the hien dung quan he, chon theo ngu canh. '
    'Da ro chu the va dien bien thi tiep cau tu nhien. Tranh lap ten o moi cau, '
    'lap "bat ngo", "vo cung", "nhanh chong" va tu tong ket sau moi nhom. '
    'Giong ke gan gui, co nhip ngan dai. Chi binh luan tren thong tin da duoc facts xac nhan. '
    'Khong chep cach dien dat tu video tham khao. '
)


def story_outline(plan):
    if plan.get('editorial_story_spine'):
        return copy.deepcopy(plan['editorial_story_spine'])
    events = [e for group in plan.get('events_by_part') or [] for e in group]
    chapters = []
    for event in events:
        beat = event.get('story_beat_id') or event['event_id']
        if not chapters or chapters[-1]['beat'] != beat:
            chapters.append(dict(
                beat=beat, first_event_id=event['event_id'],
                summary=event.get('story_beat_summary') or event.get('event') or event.get('narration_focus'),
                before=event.get('state_before') or event.get('story_beat_state_before'),
                goal=(event.get('goal') or (event.get('scene_context') or {}).get('goal')
                      or event.get('story_beat_goal')),
                cause=event.get('cause') or event.get('story_beat_cause'),
            ))
        chapters[-1].update(last_event_id=event['event_id'])
        outcome = (event.get('outcome') or event.get('state_after') or event.get('result')
                   or (event.get('story_beat_outcome') if event.get('story_beat_closing') else '')
                   or (event.get('story_beat_state_after') if event.get('story_beat_closing') else ''))
        if outcome:
            chapters[-1]['outcome'] = outcome
        else:
            chapters[-1].setdefault('outcome', '')
    return chapters


def upcoming_story_context(plan):
    events = [event for group in plan.get('events_by_part') or [] for event in group]
    cards = [{
        'event_id': event['event_id'],
        'event': event.get('event') or event.get('narration_focus'),
        'relation': (event.get('transition_contract') or {}).get('relation'),
        'opening': (event.get('transition_contract') or {}).get('current_start_state'),
    } for event in events]
    return {card['event_id']: cards[index + 1:index + 3]
            for index, card in enumerate(cards)}


def story_prompt(events, context, previous=None, error=None):
    if events and all(e.get('event_cluster_writer_version') for e in events):
        from event_cluster_writer import prompt
        return prompt(events, context, previous, error)
    if events and all(e.get('narrative_first_version') for e in events):
        from narrative_first import prompt
        return prompt(events, context, previous, error)
    from flat_writer import flat_prompt
    return (flat_prompt(events, context, previous, error) + NARRATIVE_RULE
            + '\nDAN Y TOAN PHIM, CHI KE PHAN DUOC GIAO: '
            + json.dumps(context.get('whole_story_outline') or [], ensure_ascii=False)
            + '\nViet nhu mot bai recap xuyen suot. Doc dan y toan phim truoc khi viet. '
              'Mo dau gioi thieu chu the va hoan canh bang facts. Moi doan lam ro dien bien '
              'va nguyen nhan/ket qua CHI khi facts xac nhan. Noi tiep doan DA KE, '
              'khong ke lai, khong tu tong ket moi nhom. Moi unit_id dung mot cau ngu phap. '
              'Giu nguyen quota va thu tu. Hinh duoc backend ghep sau khi chot loi ke.')


def one_sentence(text):
    from narrative_contract import sentence_texts
    fragments = sentence_texts(str(text or ''))
    clauses = [re.sub(r'[.!?]+["”’\)]*$', '', fragment).strip()
               for fragment in fragments]
    return ', '.join(c for c in clauses if c) + '.' if any(clauses) else ''


WHOLE_FILM_SCHEMA = {
    'type': 'object', 'properties': {'sentences': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'unit_id': {'type': 'string'}, 'text': {'type': 'string'}},
        'required': ['unit_id', 'text']}}},
    'required': ['sentences'],
}


def whole_film_prompt(events, context):
    from writer_contract import slots
    slots(events)
    regions = []
    total_words = 0
    for event in events:
        units = event['sentence_slots']
        region_words = sum(int(unit.get('target_words') or 20) for unit in units)
        total_words += region_words
        fact_ids = {fid for unit in units for fid in unit['fact_ids']}
        transition = event.get('transition_contract') or {}
        if regions and transition.get('bridge_unit_id') != units[0]['unit_id']:
            raise ValueError('WHOLE_FILM_BRIDGE_SLOT_INVALID:' + event['event_id'])
        regions.append({
            'region': event['event_id'],
            'target_words': region_words,
            'source_seconds': [event.get('source_region_start_seconds', units[0]['source_start']),
                               event.get('source_region_end_seconds', units[-1]['source_end'])],
            'situation': event.get('scene_context'),
            'visible_names': event.get('visible_character_names'),
            'mentioned_names': event.get('mentioned_character_names'),
            'transition': {key: transition[key] for key in ('relation', 'bridge_unit_id',
                           'previous_end_state', 'current_start_state', 'evidence_basis')
                           if key in transition},
            'facts': [{key: fact[key] for key in ('fact_id', 'text', 'evidence_kind', 'shot_ids', 'spoken_text')
                       if key in fact} for fact in event['observed_facts'] if fact['fact_id'] in fact_ids],
            'units': [{key: unit[key] for key in ('unit_id', 'fact_ids', 'shot_ids', 'source_start',
                       'source_end', 'target_words') if key in unit} for unit in units],
        })
    return (
        'Viết một bài recap tiếng Việt xuyên suốt toàn phim. Đọc toàn bộ các vùng trước khi viết. '
        'Kể mục tiêu, trở ngại, bước ngoặt và kết quả theo thứ tự phim. Mỗi câu phát triển một ý mới. '
        'Câu đầu mỗi vùng sau vùng mở đầu bắt buộc nối trạng thái cuối vùng trước với tình huống mới '
        'theo transition.bridge_unit_id. Nêu hệ quả hoặc diễn biến đồng thời chỉ khi quan hệ có căn cứ. '
        'Khi đổi nơi, thời điểm hoặc mục tiêu, giữ hành động có căn cứ giải thích bước chuyển. '
        'Nếu thiếu hành động nối trong dữ liệu, báo rõ đang chuyển sang đâu hoặc quay lại với ai. '
        'Các vùng continuation kể tiếp từ hành động đã nêu, tránh mở lại bằng một câu mô tả lặp ý. '
        'Câu nối tính trong quota và phải kể tiếp, tránh nhắc lại cả đoạn trước. '
        'Chỉ kể các mệnh đề có căn cứ trong fact_ids của chính unit. Phân biệt ai xuất hiện với ai chỉ '
        'được nhắc tới, hành động đang xảy ra với hành động đã xong, và điều thấy với điều nghe qua thoại. '
        'Đọc spoken_text đúng mốc để hiểu mục đích, quyết định và kết quả. Hình xác nhận hành động; '
        'thoại chỉ xác nhận điều nói rõ. Tách ý định, hành động, kết quả. Tránh đoán người nói hay chép thoại. '
        'Tên bảo bối, đồ vật, thiết bị và địa danh viết bằng tiếng Việt, bỏ dấu nháy. '
        'Nếu chưa có tên Việt, mô tả theo dữ kiện đã khóa. '
        f'Mục tiêu phần lời cần viết trong yêu cầu này là {context.get("batch_target_words", total_words)} từ, '
        'không tính JSON và ID. Bám mạch toàn phim; nối từ trạng thái trước, kể thay đổi của cụm '
        'rồi dẫn tới upcoming_event. Vào thẳng truyện, bỏ quảng cáo, nhạc và lời giới thiệu. '
        'Dành câu cho nguồn gốc mối nguy, phản đòn và kết quả mới. Tránh kể lại cùng một lần bị đánh. '
        'Trả JSON theo thứ tự với đúng một hàng unit_id và text '
        'cho mỗi unit, kể cả câu nối. Mỗi text là một câu hoàn chỉnh.\n'
        + json.dumps({'style': context.get('style'), 'preferences': context.get('custom_prompt'),
                      'story_outline': context.get('whole_story_outline'), 'regions': regions,
                      'upcoming_event': context.get('upcoming_event')},
                     ensure_ascii=False, separators=(',', ':')))


def _align_named_subject(line, facts, character_names):
    """Correct an explicit subject when the same action has one clear actor."""
    from identity_contract import name_in_text
    if not character_names or not isinstance(line, str):
        return line
    actor = '|'.join(re.escape(name) for name in character_names)
    pattern = re.compile(r'^\s*(?:[^,.!?]+,\s*)?(?P<name>' + actor
                         + r')\s+(?P<action>\w+\s+\w+)\b', re.I)
    draft = pattern.match(line)
    if not draft:
        return line
    action = ' '.join(draft['action'].casefold().split())
    matches = set()
    for fact in facts:
        if fact.get('evidence_kind', 'visual') != 'visual' or fact.get('status', 'confirmed') != 'confirmed':
            continue
        source_text = str(fact.get('text') or '')
        source = pattern.match(source_text)
        if source and ' '.join(source['action'].casefold().split()) == action and sum(
                name_in_text(name, source_text) for name in character_names) == 1:
            matches.add(source['name'])
    if len(matches) == 1:
        name = next(iter(matches))
        if name.casefold() != draft['name'].casefold() and not name_in_text(name, line):
            return line[:draft.start('name')] + name + line[draft.end('name'):]
    return line


def bind_whole_film_response(raw, events, characters):
    from evidence_writer import bind_planned_scenes, localize_quoted_recap_names
    from narration_units import unit_response
    from recap_visual_media import parse_writing_response
    from writer_contract import slots
    expected = slots(events)
    response = parse_writing_response(raw)
    rows = response.get('sentences') if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('WHOLE_FILM_UNIT_ORDER_INVALID: expected a sentences array')
    by_id = {}
    duplicates = set()
    for row in rows:
        uid = row.get('unit_id')
        uid = uid.strip() if isinstance(uid, str) else ''
        if uid in by_id:
            duplicates.add(uid)
        by_id[uid] = dict(row, unit_id=uid)
    missing = expected.keys() - by_id.keys()
    unexpected = by_id.keys() - expected.keys()
    if missing or unexpected or duplicates:
        raise ValueError(
            f'WHOLE_FILM_UNIT_ORDER_INVALID: missing={sorted(missing)}, '
            f'duplicate={sorted(duplicates)}, unexpected={sorted(unexpected)}')
    rows = [by_id[uid] for uid in expected]
    from identity_contract import names
    from transcript_pipeline import _is_promotional_subtitle
    character_names = sorted({name for character in characters or [] for name in names(character)},
                             key=lambda name: (-len(name), name))
    facts_by_unit = {}
    for event in events:
        facts = {fact['fact_id']: fact for fact in event['observed_facts']}
        for slot in event['sentence_slots']:
            facts_by_unit[slot['unit_id']] = [facts[fid] for fid in slot['fact_ids']]
    for row in rows:
        line = row.get('text')
        facts = facts_by_unit[row['unit_id']]
        if isinstance(line, str) and (_is_promotional_subtitle(line) or re.match(
            r'^\s*(?:xin chào (?:các bạn|mọi người)|chào mừng (?:các bạn|mọi người)|'
            r'hôm nay chúng ta sẽ (?:cùng )?(?:review|tóm tắt|xem bộ phim)|'
            r'(?:video|tập) này (?:được|do) tài trợ)\b', line, re.I)):
            row['text'] = one_sentence(' '.join(fact['text'] for fact in facts))
        elif 'text' in row:
            row['text'] = _align_named_subject(line, facts, character_names)
    draft = unit_response({'sentences': [
        dict(row, event_id=expected[row['unit_id']], text=localize_quoted_recap_names(row['text'])
             if isinstance(row.get('text'), str) else row.get('text')) for row in rows]}, events)
    for scene in draft['scenes']:
        if scene.get('format_error'):
            raise ValueError(scene['format_error'])
    return bind_planned_scenes(draft, events, characters)


def whole_film_edit_evidence(events, outline):
    """Supply the editor with grounded text and boundary relations by unit ID."""
    regions = []
    for event in events:
        units = event['sentence_slots']
        wanted = {fid for unit in units for fid in unit['fact_ids']}
        facts = {fact['fact_id']: [fact['text'], fact.get('spoken_text') or '']
                 for fact in event['observed_facts'] if fact['fact_id'] in wanted}
        transition = event.get('transition_contract') or {}
        regions.append({
            'event': event['event_id'],
            'relation': transition.get('relation'),
            'opening_unit': units[0]['unit_id'],
            'units': {unit['unit_id']: unit['fact_ids'] for unit in units},
            'facts': facts,
        })
    return ('Mỗi fact gồm [điều quan sát, lời thoại đúng mốc].\n'
            + json.dumps({'story': outline, 'regions': regions},
                         ensure_ascii=False, separators=(',', ':')))


def whole_film_edit_prompt(original_prompt, scenes, target_words):
    from narrative_contract import words
    draft = [{'event_id': scene['event_id'], 'sentences': [
        {'unit_id': proof['unit_id'], 'text': proof['text']}
        for proof in scene['sentence_proofs']]} for scene in scenes]
    actual_words = sum(words(scene['tts']) for scene in scenes)
    return (
        'Biên tập toàn bài dưới đây sau khi đọc từ đầu đến cuối. Chỉ trả các câu cần thay '
        'theo định dạng {"sentences":[{"unit_id":"ID có sẵn","text":"Một câu."}]}. '
        'Giữ thứ tự, số câu và dữ kiện, shot đã giao cho mỗi ID. '
        'Rà ai làm gì, nguyên nhân và kết quả theo hình và spoken_text; bỏ ý lặp, quảng cáo, lời giới thiệu. '
        'Kiểm tra câu cuối vùng trước và câu đầu vùng mới cùng nhau; sửa câu nối '
        'để giữ hành động hoặc quyết định có bằng chứng giải thích bước chuyển. '
        'Khi người nghe đã biết ai đang làm, dùng đại từ phù hợp hoặc lược chủ ngữ; '
        'giữ tên khi chuyển nhân vật hoặc đại từ có thể gây nhầm. '
        'Ưu tiên sai người, sai kết quả và điểm đổi tuyến; trả tối đa 64 câu sửa. '
        'Sửa câu mở đoạn để báo đang quay lại với ai; bỏ ý lặp trước khi rút bước ngoặt. '
        f'Bản hiện có {actual_words} từ, mục tiêu {target_words} từ, chấp nhận ±500 từ. '
        f'Thay đổi ròng mong muốn {target_words - actual_words:+d} từ. '
        'Nếu đã trong khoảng, ưu tiên mạch kể. Khi thiếu từ, khai thác chi tiết có căn cứ; '
        'khi thừa từ, gọn phần lặp và mô tả phụ. Trả danh sách rỗng nếu bài đã đạt.\n'
        'Kế hoạch và bằng chứng gốc:\n' + original_prompt
        + '\nBản nháp cần biên tập:\n' + json.dumps(draft, ensure_ascii=False, separators=(',', ':'))
        + '\nChỉ trả những câu đã sửa; mỗi ID xuất hiện tối đa một lần.'
    )


def read_whole_film_edits(raw):
    """Accept intact rows from an interrupted array, without guessing the tail."""
    from recap_visual_media import parse_writing_response
    try:
        return parse_writing_response(raw), False
    except ValueError:
        start = re.match(r'\s*(?:```(?:json)?\s*)?\{\s*"sentences"\s*:\s*\[', raw, re.I)
        if not start:
            raise
        decoder = json.JSONDecoder()
        rows = []
        cursor = start.end()
        while True:
            try:
                row, end = decoder.raw_decode(raw, cursor + len(raw[cursor:]) - len(raw[cursor:].lstrip()))
            except json.JSONDecodeError:
                break
            if not isinstance(row, dict):
                break
            rows.append(row)
            next_item = re.match(r'\s*,', raw[end:])
            if not next_item:
                break
            cursor = end + next_item.end()
        if not rows:
            raise
        return {'sentences': rows}, True


def bind_whole_film_edit(raw, original, events, characters, target_words, *, on_partial=None):
    from narrative_contract import words
    from narration_novelty import duplicate_sentences
    draft = {proof['unit_id']: proof['text'] for scene in original for proof in scene['sentence_proofs']}
    result, partial = read_whole_film_edits(raw)
    if partial and on_partial:
        on_partial(len(result['sentences']))
    edits = result.get('sentences') if isinstance(result, dict) else None
    if not isinstance(edits, list):
        raise ValueError('WHOLE_FILM_EDIT_SCHEMA_INVALID')
    if not edits:
        return original
    replacements = {}
    for edit in edits:
        uid = edit.get('unit_id') if isinstance(edit, dict) else None
        if not isinstance(uid, str) or uid not in draft or uid in replacements:
            raise ValueError('WHOLE_FILM_EDIT_UNIT_INVALID')
        replacements[uid] = edit.get('text')
    candidate = bind_whole_film_response(json.dumps({'sentences': [
        {'unit_id': uid, 'text': replacements.get(uid, text)} for uid, text in draft.items()
    ]}, ensure_ascii=False), events, characters)
    old_duplicates = duplicate_sentences(original)
    if any(issue not in old_duplicates for issue in duplicate_sentences(candidate)):
        raise ValueError('WHOLE_FILM_EDIT_ADDED_DUPLICATION')
    def distance(scenes):
        return max(0, abs(sum(words(scene['tts']) for scene in scenes) - target_words) - 500)
    return candidate if distance(candidate) <= distance(original) else original


def recover_events(events, characters=None):
    """Dùng facts của từng slot khi writer hết lượt, không gọi thêm AI."""
    from source_grounded_writer import bind_written_scenes
    result = []
    for event in events:
        facts = {f['fact_id']: f for f in event['observed_facts']}
        units = []
        for slot in event['sentence_slots']:
            ids = slot['fact_ids']
            text = one_sentence(' '.join(facts[fid]['text'] for fid in ids))
            units.append(dict(text=text, fact_ids=ids,
                visual_id=slot.get('visual_id') or slot['visual_ids'][0],
                visual_ids=slot.get('visual_ids') or [slot['visual_id']],
                shot_ids=slot['shot_ids'], unit_id=slot['unit_id'],
                evidence_kind=facts[ids[0]].get('evidence_kind', 'visual')))
        scene = bind_written_scenes({'scenes': [dict(event_id=event['event_id'], sentences=units)]},
                                    [event], characters)[0]
        scene.update(evidence_sentence_plan_version=1, paragraph_plan_version=2,
                     sentence_slots=copy.deepcopy(event['sentence_slots']),
                     sentence_quota=event['sentence_quota'],
                     continue_on_content_error=True,
                     narration_recovery=dict(method='locked_source_facts', verified=False))
        for index, proof in enumerate(scene['sentence_proofs'], 1):
            proof['slot_id'] = f'{event["event_id"]}_C{index:03d}'
        from source_grounded_writer import signature
        scene['source_grounded_signature'] = signature(scene)
        result.append(scene)
    return result


def restore_locked_structure(payload, locked_payload):
    """Khôi phục scene bị mất hoặc đổi ID sau lượt sửa từ bản đã khóa."""
    from narrative_contract import sentences
    result = copy.deepcopy(locked_payload)
    candidates = {s.get('scene_id'): s for p in payload.get('parts') or []
                  for s in p.get('scenes') or [] if isinstance(s, dict)}
    restored = []
    for part in result.get('parts') or []:
        for index, original in enumerate(part.get('scenes') or []):
            candidate = candidates.get(original.get('scene_id'))
            proofs = candidate.get('sentence_proofs') or [] if candidate else []
            if (candidate and len(proofs) == original.get('sentence_quota', len(original.get('sentence_proofs') or []))
                    and [p.get('unit_id') for p in proofs] ==
                        [p.get('unit_id') for p in original.get('sentence_proofs') or []]
                    and ' '.join(p.get('text', '') for p in proofs) == candidate.get('tts')
                    and all(len(sentences(p.get('text', ''))) == 1
                            for p in proofs)):
                part['scenes'][index] = copy.deepcopy(candidate)
            else:
                restored.append(original.get('scene_id'))
    return result, restored


def story_batches(events, context, max_sentences=24, max_chars=48000):
    """Size complete prompts before dispatch, reserving room for live context."""
    from writer_contract import prompt as contract_prompt, slots
    batch = []
    count = 0
    for event in events:
        slots([event])
        candidate = batch + [event]
        size = len(contract_prompt(story_prompt(candidate, context), candidate))
        if batch and (count + event['sentence_quota'] > max_sentences
                      or size > max_chars - 4000):
            yield batch
            batch, count = [], 0
        batch.append(event)
        count += event['sentence_quota']
    if batch:
        yield batch
