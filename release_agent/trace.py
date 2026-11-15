import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self, path: Path):
        self.path = path
        self._started = time.perf_counter()
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def event(self, kind: str, **data) -> None:
        record = {
            "t_s": round(time.perf_counter() - self._started, 3),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": kind,
            **data,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()
