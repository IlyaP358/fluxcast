import json
import time
from datetime import datetime, timezone
from typing import Optional


def _append_latency_log(path: Optional[str], event: str, **fields: object) -> None:
    if not path:
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "mono": round(time.monotonic(), 6),
        "event": event,
        **fields,
    }
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        pass
