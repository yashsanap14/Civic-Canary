"""Run scheduled jobs and the decision outbox once; systemd supplies the cadence."""

import asyncio
import json
import os

from services.monitoring import run_due
from services.store_factory import default_store


def main():
    if os.getenv("CIVIC_CANARY_MODE") != "aws":
        raise SystemExit("Background worker requires CIVIC_CANARY_MODE=aws")
    print(json.dumps(asyncio.run(run_due(default_store()))))


if __name__ == "__main__":
    main()
