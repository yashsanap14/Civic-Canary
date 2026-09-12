"""Optional AgentCore Runtime entrypoint; shares the EC2 production scan path."""

import asyncio

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent.civic_canary.engine import RunExecutionError
from agent.civic_canary.models import AgentInvocation
from services.runtime import ScanService
from services.store_factory import default_store

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    invocation = AgentInvocation.model_validate(payload)
    try:
        run, findings = asyncio.run(
            ScanService(default_store()).run_local(
                invocation.target,
                invocation.trigger_type,
                invocation.run_id,
            )
        )
    except RunExecutionError as exc:
        return {"run": exc.run.model_dump(mode="json"), "findings": []}
    return {
        "run": run.model_dump(mode="json"),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


if __name__ == "__main__":
    app.run()
