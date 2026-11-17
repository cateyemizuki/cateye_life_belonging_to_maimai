"""Non-billing generation evidence, independent of optional diagnostic log retention."""
from contextvars import ContextVar
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

# A mutable request scope survives wait_for's child task while staying task-local.
request_scope = ContextVar("generation_request_scope", default=None)
_last_cleanup = {}
logger = logging.getLogger(__name__)

def record_outcome(data_dir, entry):
    if not data_dir:
        return
    now = datetime.now().astimezone()
    folder = Path(data_dir) / "llm_generation_outcomes"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        allowed = ('event','module','task','model','category','group_id','request_id','tier',
                   'template_name','template_title','evidence_id','level','message','fallback_reason',
                   'completion_tokens','warning_threshold','attempt','max_tokens','used_template')
        compact = {key: value[:400] if isinstance(value,str) else value
                   for key,value in entry.items() if key in allowed}
        row = {"time": now.isoformat(timespec="seconds"), "kind": "generation", **compact}
        with (folder / ("generation_" + now.date().isoformat() + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if _last_cleanup.get(str(folder)) != now.date():
            cutoff = now.date() - timedelta(days=30)
            for path in folder.glob("generation_*.jsonl"):
                try:
                    day = datetime.strptime(path.stem.removeprefix("generation_"), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    path.unlink()
            _last_cleanup[str(folder)] = now.date()
    except (OSError, ValueError, TypeError):
        logger.warning("生成判断记录写入失败，历史判断可能显示未记录", exc_info=True)
