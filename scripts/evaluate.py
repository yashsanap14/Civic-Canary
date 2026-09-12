"""Reproducible offline contract evaluation, with optional real Bedrock evaluation.

--live-model invokes Bedrock (incurs AWS usage); HTML is a fixed local evaluation corpus.
No real-model metrics are claimed by the default offline run.
"""

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--output", default="evaluation-results.json")
    args = parser.parse_args()
    if not args.live_model:
        report = ROOT / ".evaluation-junit.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_monitoring_upgrade.py",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
            ],
            cwd=ROOT,
        )
        cases = ET.parse(report).findall(".//testcase")
        failed = sum(
            1
            for case in cases
            if case.find("failure") is not None or case.find("error") is not None
        )
        output = {
            "mode": "offline-contracts-with-mocked-model-and-AWS",
            "tests": len(cases),
            "failed": failed,
            "contract_pass_rate": (len(cases) - failed) / len(cases) if cases else None,
            "detection_accuracy": None,
            "relevance_accuracy": None,
            "false_alert_rate": None,
            "live_end_to_end_success_rate": None,
            "human_review_seconds": None,
            "note": "Contract checks do not measure model accuracy or live AWS reliability.",
        }
        Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
        raise SystemExit(result.returncode)
    from agent.civic_canary.browser import _page_from_html, _snapshot_hash
    from agent.civic_canary.models import JourneyStep, PortalSnapshot, PortalTarget
    from agent.civic_canary.reasoning import StrandsReasoner

    rows = json.loads((ROOT / "tests/fixtures/monitoring_cases.json").read_text())
    results = []
    for row in rows:

        def snapshot(html, row=row):
            page = _page_from_html(
                f'<html lang="en">{html}</html>',
                url=f"https://{row['site']}.example.org/info",
                link_status=lambda _: 200,
            )
            return PortalSnapshot(
                target_id=row["site"],
                run_id=row["name"],
                version="live",
                pages=[page],
                content_hash=_snapshot_hash([page]),
            )

        target = PortalTarget(
            target_id=row["site"],
            start_url=f"https://{row['site']}.example.org/info",
            allowed_hosts=[f"{row['site']}.example.org"],
            journey_steps=[JourneyStep(path="/", label="Evaluation")],
            monitoring_objective=row["objective"],
            guidance_context="",
        )
        started = time.perf_counter()
        try:
            _, findings, trace, _ = StrandsReasoner().execute(
                target,
                snapshot(row["before"]),
                row["name"],
                "",
                lambda row=row: snapshot(row["after"]),
            )
            results.append(
                {
                    "case": row["name"],
                    "expected": row["expected_actionable"],
                    "predicted": bool(findings),
                    "success": True,
                    "ambiguous": any(f.materiality == "NEEDS_REVIEW" for f in findings),
                    "seconds": time.perf_counter() - started,
                    "trace": trace,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "case": row["name"],
                    "expected": row["expected_actionable"],
                    "predicted": None,
                    "success": False,
                    "error": str(exc),
                }
            )
    negatives = [r for r in results if not r["expected"]]
    output = {
        "mode": "live-Bedrock-fixed-HTML-corpus-not-live-browser",
        "cases": results,
        "detection_accuracy": sum(r["predicted"] == r["expected"] for r in results) / len(results),
        "false_alert_rate": sum(r["predicted"] is True for r in negatives) / len(negatives),
        "model_workflow_success_rate": sum(r["success"] for r in results) / len(results),
        "human_review_seconds": None,
        "live_browser_success_rate": None,
    }
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
