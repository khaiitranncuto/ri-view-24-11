import json
import os
import threading
import time
import uuid
import hashlib


WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
JOB_DIR = os.path.join(WORKSPACE_DIR, ".jobs")
_write_lock = threading.RLock()


def _safe_task_id(task_id):
    value = str(task_id or "")
    if not value or any(ch not in "0123456789abcdef-" for ch in value.lower()):
        raise ValueError("task_id khong hop le")
    return value


def task_state_path(task_id):
    return os.path.join(JOB_DIR, f"{_safe_task_id(task_id)}.json")


def task_payload_path(task_id):
    return os.path.join(JOB_DIR, f"{_safe_task_id(task_id)}.payload.json")


def _atomic_write_json(path, data):
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Flask and the detached render worker are separate processes; a PID-
        # only name can still collide between heartbeat/log writes on Windows.
        temp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
                output.flush()
                os.fsync(output.fileno())
            # Browser polling can briefly keep the old JSON open on Windows.
            for attempt in range(40):
                try:
                    os.replace(temp_path, path)
                    return
                except PermissionError:
                    if attempt == 39:
                        raise
                    time.sleep(min(0.05 * (attempt + 1), 0.25))
        finally:
            # Failed serialization, full disks and replace failures all leave
            # the previous checkpoint intact and must not accumulate temp files.
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass



def save_task_state(task_id, state):
    snapshot = dict(state)
    snapshot["task_id"] = _safe_task_id(task_id)
    snapshot["updated_at"] = time.time()
    with _write_lock:
        _save_state_record(task_id, snapshot)
    return snapshot


def _save_state_record(task_id, state):
    """Publish immutable large fields before atomically publishing their references."""
    record = dict(state)
    references = dict(record.pop('_task_store_fields', {}))
    for key, value in list(record.items()):
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        references.pop(key, None)
        if len(encoded) < 65536:
            continue
        digest = hashlib.sha256(encoded).hexdigest()
        directory = os.path.join(JOB_DIR, _safe_task_id(task_id) + '.fields')
        path = os.path.join(directory, digest + '.json')
        if not os.path.isfile(path):
            _atomic_write_json(path, value)
        references[key] = digest
        del record[key]
    if references:
        record['_task_store_fields'] = references
    _atomic_write_json(task_state_path(task_id), record)


def save_task_patch(task_id, fields):
    """Update changed fields without loading or rewriting large source archives."""
    with _write_lock:
        try:
            with open(task_state_path(task_id), encoding='utf-8') as source:
                record = json.load(source)
        except FileNotFoundError:
            record = {}
        record.update(fields)
        record['task_id'] = _safe_task_id(task_id)
        record['updated_at'] = time.time()
        _save_state_record(task_id, record)


def load_task_state(task_id):
    try:
        with open(task_state_path(task_id), "r", encoding="utf-8") as source:
            value = json.load(source)
        if not isinstance(value, dict):
            return None
        for key, digest in value.pop('_task_store_fields', {}).items():
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                raise ValueError('Invalid task field reference')
            path = os.path.join(JOB_DIR, _safe_task_id(task_id) + '.fields', digest + '.json')
            with open(path, encoding='utf-8') as field:
                value[key] = json.load(field)
        return value
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_task_payload(task_id, payload):
    _atomic_write_json(task_payload_path(task_id), payload)


def load_task_payload(task_id):
    with open(task_payload_path(task_id), "r", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError("Payload render khong hop le")
    return payload
