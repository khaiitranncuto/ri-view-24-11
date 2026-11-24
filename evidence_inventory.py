"""Read-only inventory and adaptive review windows for source observations."""
import math


def detail_review_record(window, previous=None, *, status, new_facts=0, error=None):
    """Persist attempts independently from successfully reviewed intervals."""
    return dict(start_seconds=float(window['start_seconds']), end_seconds=float(window['end_seconds']),
                status=status, attempts=int((previous or {}).get('attempts', 0)) + 1,
                new_facts=max(0, int(new_facts)), error=str(error) if error else None)


def detail_stop_reason(current, required, stagnant, failures):
    if failures >= 3:
        return 'consecutive_failures'
    if stagnant >= 6:
        return 'reserve_stalled' if current >= required else 'required_evidence_stalled'
    return None


def inventory(chunks):
    events = [e for c in chunks for e in c.get('events') or []]
    facts = [f for e in events for f in e.get('grounded_facts') or []]
    return dict(events=len(events), facts=len(facts),
                source_shots=len({s for f in facts for s in f.get('shot_ids') or []}))


def observation_targets(boundaries, outline, sentence_target):
    """Budget each clip's non-overlapping core with reserve for fact filtering."""
    beats = (outline or {}).get('story_beats') or []
    weights = []
    for start, end in zip(boundaries, boundaries[1:]):
        important = max((max(0, min(end, beat['end_seconds']) - max(start, beat['start_seconds']))
                         for beat in beats if beat.get('story_role') in ('climax', 'resolution')
                         or beat.get('importance') == 'critical'), default=0)
        beat_starts = sum(start <= beat['start_seconds'] < end for beat in beats)
        weights.append(end - start + important + min(end - start, 30 * beat_starts))
    total_weight = sum(weights)
    return [(max(1, math.ceil(sentence_target * weight / total_weight)),
             max(3, math.ceil(sentence_target * 2 * weight / total_weight)))
            for weight in weights]


def detail_windows(chunks, duration, target, limit=None, seconds=60, reviewed=None,
                   prefer_story=False, outline=None):
    """Return sparse unreviewed intervals, favoring key story beats when supplied.

    The primary 2-FPS pass is independent from recap duration.  This scheduler
    is only a recovery mechanism when that pass compressed a long interval too
    aggressively.  It may cover the whole film; it never fabricates facts.
    """
    if inventory(chunks)['facts'] >= target:
        return []
    reviewed = {
        (round(float(row[0]), 3), round(float(row[1]), 3))
        for row in (reviewed or []) if isinstance(row, (list, tuple)) and len(row) == 2
    }
    beats = (outline or {}).get('story_beats') or []
    story_events = sorted(
        [e for chunk in chunks for e in chunk.get('events') or []
         if e.get('grounded_facts')],
        key=lambda e: float(e['start_seconds']),
    ) if prefer_story else []
    turning_points = []
    covered_until = 0
    for event in story_events:
        start = float(event['start_seconds'])
        if covered_until and start - covered_until >= 18 and event.get('story_role') in ('resolution', 'ending'):
            turning_points.append((covered_until, start))
        covered_until = max(covered_until, float(event['end_seconds']))
    candidates = []
    for start in range(0, math.ceil(duration), seconds):
        end = min(duration, start + seconds)
        if end - start < 5:
            continue
        key = (round(float(start), 3), round(float(end), 3))
        if key in reviewed:
            continue
        events = [e for c in chunks for e in c.get('events') or []
                  if float(e.get('start_seconds', 0)) < end
                  and float(e.get('end_seconds', 0)) > start]
        facts = {f.get('fact_id') for e in events for f in e.get('grounded_facts') or []}
        important = any(beat['start_seconds'] < end and beat['end_seconds'] > start
                        and (beat.get('story_role') in ('climax', 'resolution')
                             or beat.get('importance') == 'critical') for beat in beats)
        transition_gap = max(
            (right - left for left, right in turning_points
             if min(end, right) - max(start, left) >= 12),
            default=0,
        )
        priority = (
            -transition_gap,
            prefer_story and not events and not important,
            len(facts) - (2 if important else 0),
            start,
        )
        candidates.append((priority, dict(start_seconds=start, end_seconds=end,
                                          known_fact_count=len(facts),
                                          reason=('possible_missing_resolution' if transition_gap else
                                                  'sparse_story_details' if events
                                                  else 'missing_story_observations'))))
    ordered = [window for _, window in sorted(candidates, key=lambda candidate: candidate[0])]
    return ordered if limit is None else ordered[:max(0, int(limit))]
