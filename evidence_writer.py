"""Budget coherent situation paragraphs; split and bind sentences after writing."""
import copy
import hashlib
import json
import re

from narrative_contract import sentences, sentence_texts
from semantic_visuals import visual_catalog, timecode, seconds
from source_grounded_writer import bind_written_scenes, signature

VERSION = 1

VIETNAMESE_WRITER_RULE = (
    'QUY TAC LOI KE BAT BUOC (RECAP_DIRECT_DIALOGUE): Whisper/spoken_text chi la bang chung, '
    'khong phai loi doc. Chuyen noi dung thoai thanh loi ke gian tiep ngoi thu ba. '
    'Vi du: Anh ta noi: "Toi se quay lai" -> Anh ta cho biet minh se quay lai. '
    'Khong dong vai nhan vat, khong chep cau thoai, khong dung dau nhay hay gach dau dong '
    'trong text/paragraph/tts/narration_units. Khong chi xoa dau nhay: phai viet lai '
    'dai tu va cach dien dat thanh loi nguoi ke, giu dung y va bang chung. '
    'Khong tu gan nguoi noi neu nguon chua xac nhan. Tu kiem tra tung cau truoc khi tra JSON.\n'
    'NGÔN NGỮ BẮT BUỘC: Toàn bộ lời recap trong text, paragraph hoặc tts '
    'phải bằng tiếng Việt có dấu. CHỈ tên người/nhân vật được giữ nguyên tiếng Anh. '
    'Tên bảo bối, đồ vật, địa danh, tổ chức và mọi phần lời kể phải bằng tiếng Việt. '
    'Tên tiếng Anh trong fact/spoken_text không phải ngoại lệ. Dùng tên tiếng Việt '
    'đã được nguồn xác nhận; nếu chưa có, dùng mô tả chức năng/đặc điểm tiếng Việt '
    'đúng bằng chứng, không bịa tên hoặc công dụng. Ví dụ Rescue Bottle chỉ được '
    'gọi là bảo bối tìm kiếm khi nguồn xác nhận chức năng tìm kiếm. Không đặt tên '
    'đồ vật trong dấu nháy. Quy tắc này ưu tiên hơn yêu cầu giữ tên riêng từ nguồn. '
    'Dù dữ kiện nguồn, ngữ cảnh '
    'hoặc bản trước bằng tiếng Anh, vẫn kể lại bằng tiếng Việt. '
    'Trước khi trả JSON, tự kiểm tra mọi đơn vị lời kể và chuyển mọi đoạn '
    'tiếng Anh sang tiếng Việt, giữ nguyên ý nghĩa, ID, số đơn vị và bằng chứng.\n'
)


def require_vietnamese_narration(text, event_id):
    from recap_pipeline import vietnamese_narration_issues
    issues = vietnamese_narration_issues(text)
    if issues:
        raise ValueError('RECAP_LANGUAGE_INVALID:' + event_id + ':' + ','.join(issues)
            + '; Viết lại lời kể bằng tiếng Việt có dấu, giữ nguyên ID, '
              'số đơn vị, ý nghĩa và bằng chứng; không sao chép bản tiếng Anh.')


def has_direct_dialogue(text):
    """Narrator-only recap does not contain quoted speech or dialogue turns."""
    text = str(text or '')
    return bool(re.search(r'"[^"\n]*\w[^"\n]*"|“[^”\n]*\w[^”\n]*”|«[^»\n]*\w[^»\n]*»|‘[^’\n]*\w[^’\n]*’|(?<!\w)\x27[^\x27\n]*\w[^\x27\n]*\x27(?!\w)', text)
                or re.search(r'(?:^|\n)\s*[—–-]\s+\S', text))


QUOTED_RECAP_NAME = re.compile(
    r'\b(?P<label>bảo bối|đồ vật|thiết bị|địa danh)\s+'
    r'(?:"(?P<double>[^"\n]+)"|“(?P<curly>[^”\n]+)”|\x27(?P<single>[^\x27\n]+)\x27|‘(?P<soft>[^’\n]+)’)',
    re.IGNORECASE,
)


def localize_quoted_recap_names(text):
    """Keep a Vietnamese name or fall back to the category stated in the draft."""
    def replace(match):
        name = next(match[key] for key in ('double', 'curly', 'single', 'soft') if match[key]).strip()
        if re.search(r'[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]', name, re.I):
            return f"{match['label']} {name}"
        return match['label']

    return QUOTED_RECAP_NAME.sub(replace, text)


def require_narrator_text(text, event_id):
    # A quoted object name needs localization, not dialogue rewriting.
    if QUOTED_RECAP_NAME.search(text):
        raise ValueError('RECAP_NAME_LOCALIZATION:' + event_id
            + ':Tên đồ vật/địa danh trong dấu nháy phải viết bằng tiếng Việt, bỏ dấu nháy; '
              'chỉ tên người được giữ tiếng Anh. Dùng mô tả đúng bằng chứng nếu chưa có tên Việt.')
    if has_direct_dialogue(text):
        raise ValueError('RECAP_DIRECT_DIALOGUE:' + event_id
                         + ':rewrite as narrator-only recap; do not quote or copy Whisper')


def unsupported_dialogue_fact(fact):
    """Speech content cannot be established by a picture of someone talking."""
    text = str(fact.get('text') or '')
    speech = fact.get('evidence_kind') == 'dialogue' or bool(re.search(
        r'\b(?:giải thích|nói rằng|cho biết rằng|kể về|tiết lộ rằng|thông báo rằng)\b', text, re.I))
    return speech and not str(fact.get('spoken_text') or '').strip()


def rejected_cached_events(rows, audit_cache, source_identity=None):
    """A structurally valid repair is not reusable after a factual rejection."""
    rejected = set()
    from transition_contract import narration_boundary, boundary_audit_signature
    from narration_novelty import novelty_contexts, novelty_signature
    valid_rows = [r for r in rows or [] if isinstance(r, dict)]
    contexts = novelty_contexts(valid_rows)
    previous = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        from identity_contract import audit_signature
        key = audit_signature(row, source_identity)
        key = boundary_audit_signature(key, narration_boundary(previous))
        if row.get('evidence_sentence_plan_version') or row.get('narration_unit_version'):
            key = novelty_signature(key, contexts[row.get('scene_id') or row.get('event_id')])
        previous = row
        verdict = audit_cache.get(key) or {}
        if verdict.get('faithful') is False:
            rejected.add(row.get('event_id'))
    return rejected


def supported_facts(event):
    """Uncertain/inferred observations cannot reserve spoken sentences."""
    result, seen = [], set()
    for fact in event.get('observed_facts') or []:
        if not isinstance(fact, dict):
            continue
        if unsupported_dialogue_fact(fact):
            continue
        if (fact.get('status', 'confirmed') != 'confirmed'
                or fact.get('evidence_kind', 'visual') not in ('visual', 'dialogue')):
            continue
        text = re.sub(r'\s+', ' ', str(fact.get('text') or '')).strip()
        key = re.sub(r'[^\w\s]', '', text.casefold()).strip()
        if not text or not fact.get('fact_id') or not fact.get('shot_ids') or key in seen:
            continue
        seen.add(key)
        result.append(fact)
    return result


def sentence_slots(event):
    sources = [dict(s, start=s.get('start') or timecode(float(s['start_seconds'])),
                    end=s.get('end') or timecode(float(s['end_seconds'])))
               for s in event.get('visual_sources') or []]
    visuals = visual_catalog(sources)
    candidates = []
    for fact in supported_facts(event):
        owners = [v for v in visuals if set(v['shot_ids']) & set(fact['shot_ids'])]
        if owners:
            visual = min(owners, key=lambda v: v['start_seconds'])
            candidates.append((visual['start_seconds'], fact, visual))
    candidates.sort(key=lambda row: row[0])
    quota = int(event['sentence_quota'])
    if quota < 1 or len(candidates) < quota:
        raise ValueError(f'SENTENCE_PLAN_EVIDENCE_SHORTAGE:{event["event_id"]}:{len(candidates)}/{quota}')
    # Select distinct factual ideas across the situation, including its start
    # and result. Unselected details remain context, never extra filler clauses.
    positions = ([0] if quota == 1 else
                 [round(i * (len(candidates) - 1) / (quota - 1)) for i in range(quota)])
    result = []
    for index, position in enumerate(positions, 1):
        _, fact, visual = candidates[position]
        kind = fact.get('evidence_kind', 'visual')
        if kind == 'dialogue' and not (fact.get('spoken_text') or event.get('spoken_text')):
            raise ValueError('SENTENCE_PLAN_DIALOGUE_MISSING:' + fact['fact_id'])
        result.append(dict(slot_id=f'{event["event_id"]}_C{index:03d}', focus=fact['text'],
            fact_ids=[fact['fact_id']], visual_id=visual['visual_id'], evidence_kind=kind,
            **({'spoken_text': fact.get('spoken_text') or event.get('spoken_text')}
               if kind == 'dialogue' else
               ({'spoken_text': fact['spoken_text']} if fact.get('spoken_text') else {}))))
    return result


def build_sentence_plan(plan):
    result = copy.deepcopy(plan)
    total = 0
    for part in result['events_by_part']:
        for event in part:
            if not supported_facts(event):
                raise ValueError('SENTENCE_PLAN_EVIDENCE_MISSING:' + event['event_id'])
            # Positions only: the writer organizes the COMPLETE situation as a
            # paragraph, then the backend splits and binds the finished prose.
            event['sentence_slots'] = [dict(slot_id=f'{event["event_id"]}_C{i:03d}')
                for i in range(1, int(event['sentence_quota']) + 1)]
            event['paragraph_plan_version'] = 2
            event['evidence_sentence_plan_version'] = VERSION
            total += len(event['sentence_slots'])
    expected = int(result['sentence_contract']['target_count'])
    if total != expected:
        raise ValueError(f'EXACT_SENTENCE_BUDGET_MISMATCH:{total}/{expected}')
    result['sentence_plan_version'] = 2
    return result


def paragraph_card(event):
    source_order = {}
    for source in event.get('visual_sources') or []:
        start = (float(source['start_seconds']) if source.get('start_seconds') is not None
                 else seconds(source.get('start', 0)))
        for sid in source.get('shot_ids') or [source.get('shot_id')]:
            if sid:
                source_order[sid] = min(start, source_order.get(sid, start))
    ordered = sorted(supported_facts(event), key=lambda f: min(
        (source_order.get(sid, float('inf')) for sid in f.get('shot_ids') or []), default=float('inf')))
    facts = [dict(fact_id=f['fact_id'], text=f['text'],
                  source_order=i, evidence_kind=f.get('evidence_kind', 'visual'),
                  **({'actor_ids': f['canonical_actor_ids']} if f.get('canonical_actor_ids') else {}))
             for i, f in enumerate(ordered, 1)]
    context = list(dict.fromkeys(str(f.get('spoken_text') or '').strip()
        for f in supported_facts(event) if str(f.get('spoken_text') or '').strip()))
    if not context and event.get('spoken_text'):
        context = [str(event['spoken_text'])]
    return dict(event_id=event['event_id'], sentence_quota=int(event['sentence_quota']),
        main_event=event.get('event') or event.get('narration_focus') or '',
        target_words=event.get('tts_target_words', 20 * int(event['sentence_quota'])),
        scene_context=event.get('scene_context') or {},
        narrative_role=event.get('story_role') or event.get('narrative_function') or 'development',
        allowed_names=event.get('allowed_character_names') or [], facts=facts,
        identity_contract=copy.deepcopy(event.get('identity_contract') or {}),
        **({'transition_contract': copy.deepcopy(event['transition_contract'])}
           if event.get('transition_contract') else {}),
        **({'transcript_context_only': context} if context else {}))


def paragraph_prompt(events, context, previous=None, error=None):
    from coherent_recap import NARRATIVE_RULE
    from transition_contract import FIRST_SENTENCE_BRIDGE_RULE
    from character_identity import render_identity_context
    people = context.get('characters') or []
    summaries = [render_identity_context(str(s)[:600], people) for s in context.get('all_part_summaries') or []]
    following = [dict(row, event=render_identity_context(row.get('event', ''), people))
                 for row in context.get('next_batch_context') or [] if isinstance(row, dict)]
    return (
        VIETNAMESE_WRITER_RULE + NARRATIVE_RULE + FIRST_SENTENCE_BRIDGE_RULE +
        'VIET RECAP THEO CUM SU KIEN, MOI CUM LA MOT DOAN KE LIEN MACH. '
        'CHI VIET LOI NGUOI KE, KHONG CHEP HOI THOAI, KHONG TRICH DAN TRUC TIEP. '
        'Whisper/transcript_context_only CHI GIUP HIEU COT TRUYEN VA NGU CANH; '
        'khong chep cau Whisper vao recap, khong dat loi nhan vat trong ngoac kep, '
        'khong viet dang kich ban doi thoai. Neu can y nghia loi noi de hieu mach '
        'thi dien dat gian tiep bang loi nguoi ke, chi khi nguon ro rang. '
        'Phien am vo nghia, nhieu hoac mau thuan thi bo qua; khong doan lai cau noi. '
        'Doc toan bo facts va loi thoai cua cum truoc khi viet; khong viet cac cau roi rac roi ghep lai. '
        'Moi event_id tra paragraph co DUNG sentence_quota cau hoan chinh. '
        'So cau quyet dinh DO CHI TIET: it cau thi co dong cac dien bien lien quan; '
        'nhieu cau thi ke ro nhung chi tiet that su co trong facts/loi thoai. '
        'Mot fact co the chua nhieu chi tiet de ke bang nhieu cau; khong bat moi fact '
        'bang mot cau va khong tao them su kien de du quota. '
        'Moi cau phai them thong tin khac, khong dien dat lai cung mot y de lap cho trong. '
        'Giu cac mat xich quan trong va thu tu dien bien: nguyen nhan da xac nhan, '
        'hanh dong, ket qua. Khong bia quan he nhan qua, noi tam, nguoi noi hay hanh dong. '
        'scene_context chi de hieu mach, khong tu no chung minh mot tinh tiet moi. '
        'Dau doan noi tiep DA KE; cuoi doan dan toi TIEP THEO khi co co so. '
        'Dung ten/dai tu nhat quan, doi tuyen thi gioi thieu lai chu the. '
        'Khong ep "vi vay", "dung luc do" hay "tuy nhien" khi bang chung khong chung minh. '
        'Hinh chung minh hanh dong va dien bien. Transcript chi la ngu canh, '
        'khong chung minh nhan vat/hinh anh/dong tac dang xuat hien. '
        'evidence_kind=dialogue chi danh dau nguon cho y ke GIAN TIEP da ro nghia; '
        'khong phai cho phep trich loi thoai, suy doan nguoi noi hay noi tam. '
        'Facts co source_order theo thu tu nguon: ke dien bien theo thu tu do, '
        'khong quay lai hanh dong truoc sau khi da ke ket qua/phan ung ve sau. '
        'Khong tu them ket qua chien thang, cam xuc hay quyet dinh roi di khi facts khong co. '
        'Phan ket la uu tien thap: cum narrative_role=ending chi ke ngan gon '
        'ket qua that su duoc facts chung minh, khong tong ket dai, khong rut bai hoc, '
        'khong tao them loi chia tay hay ep co ket cuc khi nguon chua ro. '
        'evidence_kind cua cau phai dung loai fact da dan: khong gan dialogue '
        'cho fact visual chi vi transcript_context_only co loi noi gan canh. '
        'target_words la goi y, khong them cau dem de du tu. '
        'Viet xong paragraph moi liet ke sentence_evidence THEO THU TU CAC CAU, '
        'sentence_evidence phai co DUNG sentence_quota muc: moi CAU mot muc, '
        'khong phai moi fact hay moi loai bang chung mot muc. '
        'Vi du quota=1 thi paragraph co 1 cau va sentence_evidence co 1 muc. '
        'Neu mot cau can ca hinh va loi thoai nhung schema chi cho mot evidence_kind, '
        'hay viet lai cau de noi dung duoc mot loai bang chung ho tro, khong them muc. '
        'moi muc co fact_ids va evidence_kind (visual/dialogue). Cac cau ke chi tiet '
        'khac nhau duoc tham chieu cung fact_id khi fact thuc su ho tro cac chi tiet do. '
        'Khong dua ID hay timestamp vao paragraph; khong tao visual_id/trigger. '
        'Chi tra JSON scenes gom event_id, paragraph, sentence_evidence.\n'
        'PHONG CACH: ' + str(context.get('style') or '') + '\n'
        'YEU CAU: ' + str(context.get('custom_prompt') or '') + '\n'
        'MACH TRUYEN: ' + json.dumps(summaries, ensure_ascii=False) + '\n'
        'DA KE: ' + json.dumps([{'tts': str(r.get('tts') or '')[-600:]}
            for r in (context.get('previous_part_tail') or [])[-3:]], ensure_ascii=False) + '\n'
        'TIEP THEO: ' + json.dumps(following, ensure_ascii=False) + '\n'
        'CAC CUM: ' + json.dumps([paragraph_card(e) for e in events], ensure_ascii=False)
        + ('\nSUA RIENG CUM LOI, GIU SO CAU: ' + str(error) + '\nBAN TRUOC: '
           + json.dumps(previous, ensure_ascii=False) if error else ''))


def bind_paragraph(row, event, characters=None):
    """Split only AFTER writing; bind each claim to existing source evidence."""
    from recap_pipeline import _narration_text_normalizer
    paragraph = row.get('paragraph')
    if not isinstance(paragraph, str) or re.search(r'\[(?:TIME|BEAT|SHOT)=', paragraph, re.I):
        raise ValueError('PARAGRAPH_TEXT_INVALID:' + event['event_id'])
    paragraph = _narration_text_normalizer(characters or [])(paragraph)
    require_vietnamese_narration(paragraph, event['event_id'])
    require_narrator_text(paragraph, event['event_id'])
    unit_mode = bool(event.get('narration_unit_version'))
    if unit_mode:
        raw_units = row.get('narration_units') or []
        if [u.get('unit_id') for u in raw_units] != [u['unit_id'] for u in event['sentence_slots']]:
            raise ValueError('NARRATION_UNIT_IDS_INVALID:' + event['event_id'])
        texts = [_narration_text_normalizer(characters or [])(u['text']) for u in raw_units]
        for text, unit in zip(texts, raw_units):
            require_vietnamese_narration(text, event['event_id'] + '/' + unit['unit_id'])
        if ' '.join(texts) != paragraph:
            raise ValueError('NARRATION_UNIT_TEXT_MISMATCH:' + event['event_id'])
    else:
        texts = sentence_texts(paragraph)
    quota = int(event['sentence_quota'])
    if len(texts) != quota or (not unit_mode and len(sentences(paragraph)) != quota):
        raise ValueError(f'PARAGRAPH_SENTENCE_COUNT:{event["event_id"]}:expected={quota}:actual={len(texts)}')
    keys = [re.sub(r'[^\w\s]', '', t.casefold()).strip() for t in texts]
    if len(set(keys)) != len(keys):
        raise ValueError('PARAGRAPH_REPEATED_SENTENCE:' + event['event_id'])
    evidence = row.get('sentence_evidence')
    if not isinstance(evidence, list) or len(evidence) != quota:
        actual = len(evidence) if isinstance(evidence, list) else 'not_list'
        raise ValueError(f'PARAGRAPH_EVIDENCE_COUNT:{event["event_id"]}:expected={quota}:actual={actual}; '
                         'one evidence item per sentence, not per fact or evidence kind')
    facts = {f['fact_id']: f for f in supported_facts(event)}
    sources = [dict(s, start=s.get('start') or timecode(float(s['start_seconds'])),
                    end=s.get('end') or timecode(float(s['end_seconds'])))
               for s in event.get('visual_sources') or []]
    visuals = visual_catalog(sources)
    units, previous_start = [], -1.0
    for sentence_index, (text, proof) in enumerate(zip(texts, evidence), 1):
        if not isinstance(proof, dict):
            raise ValueError('PARAGRAPH_PROOF_INVALID:' + event['event_id'])
        ids = proof.get('fact_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(fid, str) or fid not in facts for fid in ids):
            raise ValueError('PARAGRAPH_PROOF_INVALID:' + event['event_id'])
        proof_shots = {sid for fid in ids for sid in facts[fid]['shot_ids']}
        cited = [v for v in visuals if set(v['shot_ids']) & proof_shots]
        kind = proof.get('evidence_kind')
        if kind == 'dialogue' and not any(facts[fid].get('evidence_kind') == 'dialogue'
                and str(facts[fid].get('spoken_text') or '').strip() for fid in ids):
            raise ValueError(f'PARAGRAPH_EVIDENCE_KIND_MISMATCH:{event["event_id"]}:sentence={sentence_index}; '
                f'fact_ids={ids}; dialogue requires a cited dialogue fact with clear source speech; '
                'Whisper is context only. Rewrite unsupported claims from the actual facts, do not relabel them.')
        if unit_mode:
            slot = event['sentence_slots'][sentence_index - 1]
            selected = [v for v in visuals if v['visual_id'] in slot['visual_ids']]
            selected.sort(key=lambda v: v['start_seconds'])
            if not selected or selected[0]['start_seconds'] < previous_start:
                raise ValueError('NARRATION_UNIT_SOURCE_ORDER_INVALID:' + slot['unit_id'])
            visual = selected[0]
        else:
            owners = [v for v in cited if v['start_seconds'] >= previous_start]
            if not owners and kind == 'dialogue' and cited:
                # A supported indirect statement may use a nearby reaction image;
                # it must not force the picture track to replay an earlier action.
                limit = max(v['end_seconds'] for v in cited) + 6
                owners = [v for v in visuals if previous_start <= v['start_seconds'] <= limit]
            if not owners:
                times = [(v['start_seconds'], v['end_seconds']) for v in cited]
                raise ValueError(f'SOURCE_WRITER_ACTION_ORDER_INVALID:{event["event_id"]}:sentence={sentence_index}; '
                    f'fact_ids={ids}; previous_source_start={previous_start}; cited_source_windows={times}; '
                    'rewrite this paragraph in source_order using supported facts; do not invent an outcome or change evidence labels')
            visual = min(owners, key=lambda v: v['start_seconds'])
            selected = [visual]
        previous_start = selected[-1]['start_seconds']
        units.append(dict(text=text, fact_ids=ids, visual_id=visual['visual_id'],
                          evidence_kind=proof.get('evidence_kind'),
                          **({'unit_id': slot['unit_id'], 'visual_ids': [v['visual_id'] for v in selected],
                              'shot_ids': slot['shot_ids']} if unit_mode else {})))
    scene = bind_written_scenes({'scenes': [{'event_id': event['event_id'], 'sentences': units}]},
        [dict(event, visual_sources=sources)], characters=characters)[0]
    scene.update(evidence_sentence_plan_version=VERSION, paragraph_plan_version=2,
                 sentence_slots=copy.deepcopy(event['sentence_slots']))
    for key in ('identity_contract', 'allowed_character_names', 'visible_character_names', 'mentioned_character_names'):
        if key in event:
            scene[key] = copy.deepcopy(event[key])
    if event.get('transition_contract'):
        scene['transition_contract'] = copy.deepcopy(event['transition_contract'])
        scene['transition_contract_version'] = event.get('transition_contract_version', 1)
        scene['relation_to_previous'] = event['transition_contract'].get('relation')
    if unit_mode:
        scene['narration_unit_version'] = 1
    for index, proof in enumerate(scene['sentence_proofs'], 1):
        proof['slot_id'] = f'{event["event_id"]}_C{index:03d}'
    scene['source_grounded_signature'] = signature(scene)
    from sentence_visual_contract import enforce_sentence_visual_contract
    enforce_sentence_visual_contract({'parts': [{'scenes': [dict(scene, sentence_quota=quota)]}]},
                                     quota, require_audit=False)
    return scene


def writing_batches(events, max_sentences=24, max_chars=8000):
    batch, count, size = [], 0, 0
    for event in events:
        slots = event['sentence_slots']
        length = len(json.dumps(paragraph_card(event) if event.get('paragraph_plan_version') == 2 else slots,
                                ensure_ascii=False))
        if batch and (count + len(slots) > max_sentences or size + length > max_chars):
            yield batch
            batch, count, size = [], 0, 0
        batch.append(event)
        count += len(slots)
        size += length
    if batch:
        yield batch


def writing_schema(events):
    if all(e.get('paragraph_plan_version') == 2 for e in events):
        # One shared shape avoids combinatorial provider-side schema branching.
        # Event-specific quotas/ownership remain enforced by bind_paragraph.
        quotas = [int(e['sentence_quota']) for e in events]
        return {'type': 'object', 'properties': {'scenes': {'type': 'array',
            'minItems': len(events), 'maxItems': len(events), 'items': {
                'type': 'object', 'properties': {
                'event_id': {'type': 'string', 'enum': [e['event_id'] for e in events]},
                'paragraph': {'type': 'string'},
                'sentence_evidence': {'type': 'array', 'minItems': min(quotas), 'maxItems': max(quotas),
                    'items': {'type': 'object', 'properties': {
                        'fact_ids': {'type': 'array', 'minItems': 1, 'items': {'type': 'string',
                            'enum': list(dict.fromkeys(f['fact_id'] for e in events for f in supported_facts(e)))}},
                        'evidence_kind': {'type': 'string', 'enum': ['visual', 'dialogue']}},
                        'required': ['fact_ids', 'evidence_kind']}}},
                'required': ['event_id', 'paragraph', 'sentence_evidence']}}}, 'required': ['scenes']}
    return {'type': 'object', 'properties': {'scenes': {'type': 'array',
        'minItems': len(events), 'maxItems': len(events), 'items': {
            'type': 'object', 'properties': {
                'event_id': {'type': 'string', 'enum': [e['event_id'] for e in events]},
                'sentences': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'slot_id': {'type': 'string', 'enum': [s['slot_id'] for e in events for s in e['sentence_slots']]},
                    'text': {'type': 'string'}}, 'required': ['slot_id', 'text']}}},
            'required': ['event_id', 'sentences']}}}, 'required': ['scenes']}


def writing_prompt(events, context, previous=None, error=None):
    if all(e.get('paragraph_plan_version') == 2 for e in events):
        return paragraph_prompt(events, context, previous, error)
    cards = [dict(event_id=e['event_id'], scene_context=e.get('scene_context') or {},
                  allowed_names=e.get('allowed_character_names') or [],
                  target_words=e.get('tts_target_words', 20 * len(e['sentence_slots'])),
                  sentences=e['sentence_slots']) for e in events]
    return (
        VIETNAMESE_WRITER_RULE +
        'VIET RECAP TIENG VIET TU KE HOACH CAU DA KHOA. Khong can xem lai video. '
        'Moi slot_id tra DUNG mot cau hoan chinh, ket thuc bang dau cham/hoi/than; '
        'giu nguyen thu tu event va slot. Tong so cau bang so slot, khong them/bot. '
        'target_words la goi y do dai cho ca cum, uu tien cau tu nhien khoang 15-25 tu '
        'khi du bang chung; khong them cau dem hay tinh tiet de keo dai. '
        'Moi cau chi dien dat focus cua slot; khong them hanh dong, nhan vat, loi thoai, '
        'noi tam hay ket qua ngoai bang chung. evidence_kind=dialogue chi chung minh '
        'noi dung duoc noi, khong chung minh hanh dong nhin thay hay nguoi noi. '
        'spoken_text la loi thoai dung vung bang chung, duoc dung de hieu/noi mach '
        'khi noi ro nguyen nhan; khong tu quy nguoi noi hay suy dien quan he nhan qua. '
        'Doc ca cum truoc khi viet: noi cac cau thanh doan ke tu nhien, tranh lap '
        'chu ngu va lap y. Chi dung quan he nguyen nhan/ket qua khi focus chung minh; '
        'khong tu noi hai tuyen bang "dung luc". Khong doc SHOT/fact/slot ID trong loi ke. '
        'Khong viet timestamp, visual_id, fact_ids hay trigger; backend se gan tu ke hoach. '
        'Chi tra JSON scenes, moi scene co event_id va sentences gom slot_id, text.\n'
        'PHONG CACH: ' + str(context.get('style') or '') + '\n'
        'YEU CAU: ' + str(context.get('custom_prompt') or '') + '\n'
        'MACH TRUYEN: ' + json.dumps([str(s)[:600] for s in context.get('all_part_summaries') or []], ensure_ascii=False) + '\n'
        'DA KE: ' + json.dumps([{'tts': str(r.get('tts') or '')[-600:]} for r in (context.get('previous_part_tail') or [])[-3:]], ensure_ascii=False) + '\n'
        'TIEP THEO: ' + json.dumps(context.get('next_batch_context') or [], ensure_ascii=False) + '\n'
        'KE HOACH: ' + json.dumps(cards, ensure_ascii=False)
        + ('\nSUA DUNG LOI: ' + str(error) + '\nBAN TRUOC: ' + json.dumps(previous, ensure_ascii=False)
           if error else ''))


def bind_planned_scenes(payload, events, characters=None):
    rows = payload.get('scenes') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or [r.get('event_id') for r in rows] != [e['event_id'] for e in events]):
        raise ValueError('SENTENCE_PLAN_EVENT_IDS_INVALID')
    bound = []
    for row, event in zip(rows, events):
        if event.get('paragraph_plan_version') == 2:
            bound.append(bind_paragraph(row, event, characters))
            continue
        units = row.get('sentences')
        slots = event['sentence_slots']
        if (not isinstance(units, list) or any(not isinstance(u, dict) for u in units)
                or [u.get('slot_id') for u in units] != [s['slot_id'] for s in slots]):
            raise ValueError('SENTENCE_PLAN_SLOT_IDS_INVALID:' + event['event_id'])
        if any(not isinstance(u.get('text'), str) or len(sentences(u['text'])) != 1 for u in units):
            raise ValueError('SENTENCE_PLAN_ONE_SENTENCE_REQUIRED:' + event['event_id'])
        for unit in units:
            require_vietnamese_narration(unit['text'], event['event_id'])
            require_narrator_text(unit['text'], event['event_id'])
        raw = {'scenes': [{'event_id': event['event_id'], 'sentences': [
            dict(text=u['text'], fact_ids=s['fact_ids'], visual_id=s['visual_id'], evidence_kind=s['evidence_kind'])
            for u, s in zip(units, slots)]}]}
        scene = bind_written_scenes(raw, [event], characters=characters)[0]
        scene['evidence_sentence_plan_version'] = VERSION
        scene['sentence_slots'] = copy.deepcopy(slots)
        for proof, slot in zip(scene['sentence_proofs'], slots):
            proof['slot_id'] = slot['slot_id']
        scene['source_grounded_signature'] = signature(scene)
        bound.append(scene)
    return bound


def _unit_repair(event, row, error):
    from narrative_contract import sentences
    from narrative_first import MATCH_AFTER_WRITING_VERSION, LOCKED_VERSION

    if (event.get('narrative_first_version') not in (MATCH_AFTER_WRITING_VERSION, LOCKED_VERSION)
            or event.get('event_cluster_writer_version') == 1):
        return None
    units = row.get('narration_units') or []
    retained = {}
    affected = set()
    expected = {slot['unit_id'] for slot in event['sentence_slots']}
    facts = {fact['fact_id'] for fact in supported_facts(event)}
    planned = {slot['unit_id']: slot['fact_ids'] for slot in event['sentence_slots']}
    for unit in units:
        uid = unit.get('unit_id')
        if uid not in expected:
            continue
        text = unit.get('text')
        ids = (planned[uid] if event.get('narrative_first_version') == LOCKED_VERSION
               else unit.get('fact_ids'))
        if (uid in retained or not isinstance(text, str) or len(sentences(text)) != 1
                or not isinstance(ids, list) or not ids
                or any(not isinstance(fid, str) or fid not in facts for fid in ids)):
            affected.add(uid)
        else:
            retained[uid] = dict(unit, fact_ids=ids)
    affected.update(expected - retained.keys())
    if str(error).startswith(('NARRATION_UNIT_VISUAL_MISSING:',
                               'NARRATION_UNIT_MIXED_SOURCE:',
                               'NARRATION_UNIT_SOURCE_ORDER_INVALID:')):
        affected.add(error.split(':', 2)[1])
    if str(error).startswith('PARAGRAPH_REPEATED_SENTENCE:'):
        seen = set()
        for unit in units:
            text = re.sub(r'[^\w\s]', '', str(unit.get('text') or '').casefold()).strip()
            if text in seen:
                affected.add(unit.get('unit_id'))
            seen.add(text)
    slots = event['sentence_slots']
    repair_slots = [slot for slot in slots if slot['unit_id'] in affected]
    if not repair_slots or len(repair_slots) == len(slots):
        return None
    neighbors = [retained[slot['unit_id']] for i, slot in enumerate(slots)
                 if slot['unit_id'] in retained and slot['unit_id'] not in affected
                 and any(s['unit_id'] in affected for s in slots[max(0, i - 1):i + 2])]
    partial = dict(event, sentence_slots=repair_slots, sentence_quota=len(repair_slots),
                   tts_target_words=sum(int(s.get('target_words') or 20) for s in repair_slots))
    previous = {'scenes': [dict(event_id=event['event_id'], narration_units=neighbors)]}
    return partial, previous, {uid: unit for uid, unit in retained.items() if uid not in affected}


def write_batch(events, context, generate, candidates=None, save=None, characters=None,
                prompt_builder=None, response_adapter=None, save_raw=None,
                capacity_error=None, on_split=None, timeout_error=None, fast_mode=False,
                on_timing=None):
    """Keep valid event rows while retrying only missing/invalid events once."""
    from narrative_first import MATCH_AFTER_WRITING_VERSION, bind_response
    from recap_visual_media import parse_writing_response
    ready, error = {}, None
    event_errors = {}
    provider_pending = {}
    drafts = list(candidates or [])
    partial_rows = {}
    def accept(payload, restoring=False):
        nonlocal error
        rows = payload.get('scenes') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            error = 'SENTENCE_PLAN_SCHEMA_INVALID'
            return
        seen = set()
        lookup = {e['event_id']: e for e in events}
        for row in rows:
            eid = row.get('event_id')
            if not isinstance(eid, str) or eid not in lookup or eid in seen:
                raise ValueError('SENTENCE_PLAN_EVENT_IDS_INVALID')
            seen.add(eid)
        for row in rows:
            eid = row['event_id']
            if eid in ready:
                continue
            if not restoring or eid not in partial_rows:
                partial_rows[eid] = row
            try:
                if row.get('format_error'):
                    raise ValueError(str(row['format_error']))
                event = lookup[eid]
                if event.get('narrative_first_version') == MATCH_AFTER_WRITING_VERSION:
                    staged = dict(event)
                    units = row.get('narration_units') or []
                    proofs = row.get('sentence_evidence') or []
                    if len(units) != len(proofs):
                        raise ValueError('NARRATION_UNIT_EVIDENCE_INVALID:' + eid)
                    bind_response({unit['unit_id']: dict(text=unit['text'], fact_ids=proof['fact_ids'])
                                   for unit, proof in zip(units, proofs)}, [staged])
                    scene = bind_planned_scenes({'scenes': [row]}, [staged], characters)[0]
                    event['sentence_slots'] = staged['sentence_slots']
                    ready[eid] = scene
                else:
                    ready[eid] = bind_planned_scenes({'scenes': [row]}, [event], characters)[0]
            except ValueError as exc:
                error = str(exc)
                event_errors[eid] = error
    for draft in reversed(drafts):
        try:
            accept(draft, restoring=True)
        except ValueError as exc:
            error = str(exc)
    def persist():
        if save:
            # A later interrupted repair must not evict valid rows from the
            # first draft merely because only two raw candidates are retained.
            completed = {'scenes': [({'event_id': e['event_id'],
                'paragraph': ready[e['event_id']]['tts'], 'sentence_evidence': [
                    {'fact_ids': p['fact_ids'], 'evidence_kind': p['evidence_kind']}
                    for p in ready[e['event_id']]['sentence_proofs']],
                **({'narration_units': [dict(unit_id=p['unit_id'], event_id=e['event_id'],
                                           text=p['text'], fact_ids=p['fact_ids'])
                    for p in ready[e['event_id']]['sentence_proofs']]} if e.get('narration_unit_version') else {})}
                if e.get('paragraph_plan_version') == 2 else
                {'event_id': e['event_id'], 'sentences': [
                    {'slot_id': p['slot_id'], 'text': p['text']}
                    for p in ready[e['event_id']]['sentence_proofs']]})
                for e in events if e['event_id'] in ready]}
            partial = {'scenes': [row for eid, row in partial_rows.items() if eid not in ready]}
            save([completed, partial])

    for _ in range(2):
        missing = [e for e in events if e['event_id'] not in ready
                   and e['event_id'] not in provider_pending]
        if not missing:
            break
        pending = [missing]
        while pending:
            group = pending.pop(0)
            group_context = copy.deepcopy(context)
            first = events.index(group[0])
            last = events.index(group[-1])
            preceding = [ready[e['event_id']] for e in events[:first] if e['event_id'] in ready]
            if preceding:
                group_context['previous_part_tail'] = (
                    list(context.get('previous_part_tail') or []) + preceding)[-2:]
            following = [dict(event=e.get('event') or e.get('narration_focus') or '')
                         for e in events[last + 1:last + 3]]
            if following:
                group_context['next_batch_context'] = following
            repair = None
            request_group = group
            try:
                repair_error = '\n'.join(event_errors[e['event_id']] for e in group
                                         if e['event_id'] in event_errors) or error
                previous_group = {'scenes': [row for draft in drafts for row in draft.get('scenes', [])
                    if row.get('event_id') in {e['event_id'] for e in group}]}
                if (fast_mode and response_adapter and len(group) > 1 and any(
                        e['event_id'] in partial_rows for e in group)):
                    pending[0:0] = [[event] for event in group]
                    continue
                if fast_mode and response_adapter and len(group) == 1:
                    eid = group[0]['event_id']
                    repair = _unit_repair(group[0], partial_rows.get(eid, {}), event_errors.get(eid))
                    if repair:
                        partial, previous_group, _ = repair
                        request_group = [partial]
                if (not fast_mode and repair_error and len(group) > 1
                        and all(e.get('narration_unit_version') for e in group)
                        and response_adapter):
                    pending[0:0] = [[event] for event in group]
                    continue
                request_prompt = (prompt_builder or writing_prompt)(
                    request_group, group_context, previous_group if drafts else None, repair_error)
                # Measure the complete request, including context and repair text.
                # Split without dropping facts; each child rebuilds its context.
                if (not fast_mode and len(group) == 1 and group[0].get('narration_unit_version')
                        and response_adapter and repair_error):
                    from narration_units import generate_unit_chunks
                    raw = generate_unit_chunks(group[0], group_context, generate,
                        prompt_builder or writing_prompt, previous_group,
                        event_errors.get(group[0]['event_id'], repair_error), save_raw=save_raw)
                elif repair:
                    raw = generate(request_group, request_prompt)
                elif len(request_prompt) > (48000 if fast_mode else 15500):
                    if len(group) > 1:
                        middle = len(group) // 2
                        children = [group[:middle], group[middle:]]
                        pending[0:0] = children
                        if on_split:
                            on_split(group, children)
                        continue
                    if group[0].get('narration_unit_version') and response_adapter:
                        from narration_units import generate_unit_chunks
                        raw = generate_unit_chunks(group[0], group_context, generate,
                            prompt_builder or writing_prompt, previous_group if drafts else None,
                            repair_error, save_raw=save_raw,
                            max_chars=48000 if fast_mode else 14000,
                            max_repairs=0 if fast_mode else 2)
                    else:
                        raise ValueError('WRITER_PROMPT_TOO_LARGE:' + group[0]['event_id']
                        + ':' + str(len(request_prompt))
                        + ':single event exceeds soft limit; preserve evidence, do not truncate')
                else:
                    raw = generate(group, request_prompt)
            except Exception as exc:
                persist()
                if isinstance(exc, ValueError) and str(exc).startswith('NARRATION_UNIT_'):
                    error = str(exc)
                    for event in group:
                        event_errors[event['event_id']] = error
                    continue
                can_split = ((capacity_error and capacity_error(exc))
                             or (timeout_error and timeout_error(exc)))
                if fast_mode and can_split and len(group) == 1:
                    provider_pending[group[0]['event_id']] = str(exc)
                    continue
                if not can_split or len(group) <= 1:
                    raise
                middle = len(group) // 2
                children = [group[:middle], group[middle:]]
                pending[0:0] = children
                if on_split:
                    on_split(group, children)
                continue
            from time import perf_counter
            validation_started = perf_counter()
            if save_raw:
                save_raw(raw, request_group)
            try:
                payload = parse_writing_response(raw)
                if repair:
                    units = dict(repair[2])
                    requested_ids = {slot['unit_id'] for slot in request_group[0]['sentence_slots']}
                    repaired_units = payload.get('sentences')
                    if (not isinstance(repaired_units, list)
                            or any(not isinstance(unit, dict) or unit.get('event_id') != group[0]['event_id']
                                   or not isinstance(unit.get('unit_id'), str)
                                   or unit.get('unit_id') not in requested_ids for unit in repaired_units)
                            or len({unit['unit_id'] for unit in repaired_units}) != len(repaired_units)):
                        raise ValueError('NARRATION_UNIT_REPAIR_RESPONSE_INVALID:' + group[0]['event_id'])
                    units.update((unit['unit_id'], unit) for unit in repaired_units)
                    payload = {'sentences': [units[slot['unit_id']] for slot in group[0]['sentence_slots']
                                             if slot['unit_id'] in units]}
                if response_adapter:
                    payload = response_adapter(payload, group)
                drafts.append(payload)
                drafts = drafts[-2:]
                accept(payload)
            except ValueError as exc:
                error = str(exc)
            checkpoint_started = perf_counter()
            persist()
            if on_timing:
                on_timing(checkpoint_started - validation_started, perf_counter() - checkpoint_started)
    # A locked plan already contains a supported fallback for each sentence.
    # Preserve accepted neighbouring prose and replace only defective units.
    from narrative_first import LOCKED_VERSION
    from coherent_recap import one_sentence
    for event in events:
        eid = event['event_id']
        if (eid in ready or eid in provider_pending
                or event.get('narrative_first_version') != LOCKED_VERSION):
            continue
        row = partial_rows.get(eid, {})
        repair = _unit_repair(event, row, event_errors.get(eid))
        retained = repair[2] if repair else {}
        facts = {fact['fact_id']: fact for fact in supported_facts(event)}
        units = []
        for slot in event['sentence_slots']:
            uid = slot['unit_id']
            text = (retained[uid]['text'] if uid in retained else one_sentence(
                ' '.join(facts[fid]['text'] for fid in slot['fact_ids'])))
            units.append(dict(event_id=eid, unit_id=uid, text=text))
        from narration_units import unit_response
        accept(unit_response({'sentences': units}, [event]))
        if eid in ready:
            ready[eid]['narration_recovery'] = dict(method='locked_source_facts', verified=False)
    persist()
    if len(ready) != len(events):
        if provider_pending:
            waiting = ','.join(provider_pending)
            other = [e['event_id'] for e in events
                     if e['event_id'] not in ready and e['event_id'] not in provider_pending]
            detail = f'; WRITER_CONTENT_PENDING:{",".join(other)}; {error}' if other else ''
            raise ValueError(f'WRITER_PROVIDER_PENDING:{waiting}{detail}')
        raise ValueError(error or 'SENTENCE_PLAN_INCOMPLETE')
    return [ready[e['event_id']] for e in events]


def text_audit_prompt(cards):
    return (
        'RANH GIOI KY THUAT KHONG PHAI DOI DIEN BIEN: cung tinh huong thi '
        'khong bat cau nao cung co tu noi hay nhac lai cum truoc. Chi yeu cau dinh huong '
        'khi doi dien bien, thoi gian, dia diem hoac tuyen ke. '
        'Doc recent_narration va tat ca cau trong tts: moi cau phai them thong tin. '
        'Trung y du doi chu: faithful=false, reason="duplicate_narration:" kem cau/scene lap. '
        'Lap mot phan thi chi bo phan lap va giu thong tin moi; cung hanh dong o lan khac '
        'co bang chung thi khong phai lap. Khong chap nhan dien dat lai de du so cau. '
        'Kiem tra trang thai HOAT DONG: da xong thi khong duoc noi dang lam; '
        'neu chua ro thi khong tu khang dinh da xong hay dong thoi. '
        'KIEM TRA MACH GIUA HAI CUM: doc previous_scene.tts va cau dau tts hien tai. '
        'Khong cham tung cum doc lap. Neu previous_scene co noi dung thi phai '
        'kiem tra quan he giua ket cum truoc va dau cum nay, ke ca qua batch. '
        'Khi doi tuyen, cau mo dau neu ro nhan vat va dia diem moi la du neu da ro nghia. '
        'Vi du "Nobita tinh day giua vung nui tuyet" sau canh Doraemon doi dau ke thu '
        'la cach dinh vi tuyen hop le; khong bat them hanh trinh di chuyen giua hai tuyen. '
        'Neu cung mot nhan vat vua o nui tuyet lai bi ke dang trong tu ma khong co bang chung '
        'bi bat/di chuyen va fact cung gan nhan vat do, bao source_actor_conflict: de xem lai hinh. '
        'Chi bao source_actor_conflict: hoac source_action_conflict: neu mau thuan nam trong fact nguon. '
        'Neu chi loi ke them cam xuc, muc dich hay nguyen nhan, bao inference: de sua bang van ban. '
        'Cung hanh trinh ma noi "trong mot dien bien khac" la noi sai. '
        'Menh de "sau khi bi danh bai, Nobita..." sai neu nguoi bi danh bai la Doraemon. '
        'Khong yeu cau bia di chuyen, dong thoi hay nhan qua de noi cho em. '
        'Neu thieu dien bien hoac sai actor: faithful=false, reason bat dau '
        '"transition_state_conflict:". Chi bao "transition_bridge_missing:" neu nguoi nghe '
        'co the hieu nham chu the, boi canh hoac dien bien. Neu da ro nghia thi chap nhan '
        'cau ke truc tiep, ke ca khi doi dia diem va khong co tu noi. '
        'KIEM CHUNG LOI KE VOI BANG CHUNG DA QUAN SAT, khong viet lai. '
        'DANH GIA Y NGHIA, chap nhan tu dong nghia va cach noi tu nhien cung su kien, '
        'vi du "san ca" va "bat ca" neu facts la dung giao bat ca. '
        'Chi bac bo khi menh de them hoac doi thong tin thuc te; neu ro thong tin nao '
        'bi them. Van bac bo noi tam, muc dich, nguyen nhan hoac loi bao truoc khong co bang chung. '
        'Moi scene_id tra faithful=true chi khi TAT CA menh de duoc observed_facts '
        'hoac spoken_text ro rang trong fact ho tro. identity_contract phan biet nguoi '
        'co mat va nguoi chi duoc nhac; nguoi chi duoc nhac khong duoc bien thanh actor '
        'dang hanh dong trong hinh. Khong coi tu thuong nhu "gian kho" la ten Gian. '
        'Ten phu/quan he/loai sinh vat phai co bang chung; chua ro dung mo ta trung tinh. '
        'ho tro; khong chap nhan suy doan nguoi, hanh dong, noi tam, nguyen nhan hay '
        'ket qua. Chi kiem tra dieu cau thuc su ke, khong bat ke het facts. '
        'Nhieu cau duoc cung tham chieu mot fact neu ke cac chi tiet khac nhau '
        'da co trong fact/loi thoai. Neu cac cau chi lap lai cung mot y bang tu khac '
        'de lap quota, tra faithful=false va neu ro phan lap; khong nham voi cac '
        'buoc hanh dong khac nhau cua cung mot su kien. '
        'Neu co transition_contract, cau chu duoc phep da dang nhung Y NGHIA chuyen mach '
        'phai dung relation va cac anchor trong contract. Contract chi ho tro moi lien he/anchor '
        'duoc ghi. requires_bridge=true: cau dau phai noi quan he da xac nhan hoac gioi thieu '
        'boi canh/chu the moi theo cach tu nhien. Neu da ro ai dang lam gi thi '
        'khong bao thieu cau noi. Khong bat mot tu noi cu the; dia diem/chu the '
        'duoc gioi thieu dung cung la cau noi. Khong suy ra thu tu thoi gian tu thu tu video; '
        'hoan thanh, dong thoi, muc dich va nhan qua deu can bang chung. Contract chi ho tro moi lien he/anchor '
        'duoc ghi, khong cho phep them chi tiet hien tai. neutral_reintroduction khong duoc ngam '
        'chi quan he truoc-sau hay nhan-qua. Voi relation nay, dinh vi ro chu the, noi chon '
        'hoac tinh huong moi bang facts la du; khong yeu cau giai thich tai sao phim cat canh. '
        'Quy tac nay uu tien hon yeu cau moc lai hanh dong cuoi khi quan he chua ro. '
        'Khong bac bo chi vi writer khong chep dung mot cum tu mau. '
        'Loi thoai chi ho tro noi dung duoc noi. Khong goi day la da kiem chung video. '
        'Recap phai la loi nguoi ke: trich hoi thoai truc tiep hoac chep phien am '
        'vo nghia tu Whisper thi faithful=false; yeu cau ke gian tiep tu bang chung ro nghia. '
        'Neu can xem hinh de xac minh chi tiet hoac quan he ma cau da khang dinh '
        'va archive_boundary_context cung chua du: faithful=false, reason bat dau '
        '"visual_review_required:". Neu cau trai/ them y so voi facts: faithful=false, '
        'reason neu ro menh de sai de sua rieng. Tra JSON '
        '{"checks":[{"scene_id":"S0001","faithful":true,"reason":""}]}.\n'
        + json.dumps(cards, ensure_ascii=False))


AUDIT_SCHEMA = {'type': 'object', 'properties': {'checks': {'type': 'array', 'items': {
    'type': 'object', 'properties': {'scene_id': {'type': 'string'}, 'faithful': {'type': 'boolean'},
        'reason': {'type': 'string'}}, 'required': ['scene_id', 'faithful', 'reason']}}}, 'required': ['checks']}


def audit_planned_payload(payload, text_check, video_check, text_cache=None, video_cache=None,
                         save_text=None, save_video=None, cache_only=False,
                         source_identity=None, source_offset=0.0, diagnostic=None,
                         event_region_mode=False):
    """Verify event clips directly, reusing per-sentence verdict checkpoints."""
    from grounded_pipeline import audit_narrations
    if event_region_mode:
        return audit_narrations(payload, video_check, checkpoint=video_cache,
            save=save_video, visual_evidence=True, source_identity=source_identity,
            visual_source_offset=source_offset, max_attempts=0 if cache_only else 1,
            diagnostic=diagnostic)
    checked = audit_narrations(payload, text_check, checkpoint=text_cache,
        save=save_text, max_attempts=0 if cache_only else 2,
        source_identity=source_identity, diagnostic=diagnostic)
    risky = {s['scene_id'] for p in checked['parts'] for s in p.get('scenes') or []
             if s.get('audit_verdict_conflict') or any(str(error).startswith(
                 'unsupported_narration:visual_review_required:')
                 for error in s.get('visual_alignment_errors') or [])}
    if risky:
        subset = copy.deepcopy(checked)
        for part in subset['parts']:
            part['scenes'] = [s for s in part.get('scenes') or [] if s['scene_id'] in risky]
        reviewed = audit_narrations(subset, video_check, checkpoint=video_cache,
            save=save_video, visual_evidence=True, source_identity=source_identity,
            visual_source_offset=source_offset, max_attempts=0 if cache_only else 1,
            diagnostic=diagnostic)
        replacements = {s['scene_id']: s for p in reviewed['parts'] for s in p.get('scenes') or []}
        for part in checked['parts']:
            part['scenes'] = [replacements.get(s['scene_id'], s) for s in part.get('scenes') or []]
    return checked
