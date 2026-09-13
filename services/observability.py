from __future__ import annotations

import json
import logging
from typing import Any

LOGGER = logging.getLogger("civic_canary")
LOGGER.setLevel(logging.INFO)


def log_event(event: str, *, run_id: str, **details: Any) -> None:
    """Emit one searchable record without secrets or form values.

    Always print to stdout so systemd/journalctl shows scan/worker events.
    Browser capture already does this; other stages previously only used the
    logger and often vanished when no handler was configured.
    """
    payload = {"event": event, "run_id": run_id, **details}
    line = json.dumps(payload, separators=(",", ":"), default=str)
    print(f"[civic_canary] {line}", flush=True)
    LOGGER.info(line)
