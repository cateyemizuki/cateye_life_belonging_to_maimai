"""Event generation cycles and nonblocking cross-process write exclusion."""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

POOL_VERSION = 2
POOL_KINDS = ('builtin', 'reusable', 'one_shot')


def allocate_pools(total: int, builtin: int, per_pool: int) -> tuple[int, int]:
    slots = max(0, int(total) - int(builtin))
    if slots < 2 or per_pool < 1:
        return 0, 0
    return min(int(per_pool), (slots + 1)//2), min(int(per_pool), slots//2)


def normalize_cycle(document: dict) -> dict:
    """Migrate legacy generated rows in memory; bad receipts never resurrect events."""
    if (not isinstance(document, dict) or type(document.get('version')) is not int
            or document.get('version') not in (1, POOL_VERSION)):
        raise ValueError('unsupported event pool version')
    doc = deepcopy(document)
    events = doc.get('events')
    if not isinstance(events, list):
        raise ValueError('events must be an array')
    legacy = doc['version'] == 1
    if legacy:
        stable = json.dumps({'events':events, 'generated_at':doc.get('generated_at')},
                            sort_keys=True, ensure_ascii=False).encode('utf-8')
        doc['generation_id'] = 'legacy_' + hashlib.sha256(stable).hexdigest()[:24]
        doc['consumed'] = {}
    generation = doc.get('generation_id')
    if not isinstance(generation, str) or not generation or len(generation) > 96:
        raise ValueError('invalid event generation identifier')
    consumed = doc.get('consumed')
    if not isinstance(consumed, dict):
        raise ValueError('consumption receipts missing or invalid')
    once_ids = set()
    for event in events:
        if not isinstance(event, dict):
            continue  # item validation remains the catalog validator's responsibility
        kind = event.get('pool_kind', 'reusable')
        if kind not in ('reusable', 'one_shot') or (legacy and kind != 'reusable'):
            raise ValueError('invalid generated pool kind')
        event['pool_kind'] = kind
        event['generation_id'] = generation
        if kind == 'one_shot':
            once_ids.add(str(event.get('event_id') or ''))
    if len(consumed) > len(once_ids):
        raise ValueError('consumption receipts exceed one-shot pool')
    for eid, receipt in consumed.items():
        if eid not in once_ids or not isinstance(receipt, dict):
            raise ValueError('unknown one-shot receipt')
        if receipt.get('generation_id') != generation or receipt.get('status') not in (
                'selected', 'started', 'start_failed', 'interrupted', 'completed'):
            raise ValueError('invalid one-shot receipt')
    doc['version'] = POOL_VERSION
    return doc


@contextmanager
def catalog_write_guard(path: Path):
    """OS releases byte locks on process death; never wait or clear an unknown lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    handle = open(lock_path, 'a+b')
    locked = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
