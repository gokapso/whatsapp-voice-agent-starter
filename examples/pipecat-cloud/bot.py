"""Pipecat Cloud session entry point for this starter.

Kapso's dashboard Pipecat provider starts one session per WhatsApp call. The session body is
handled by kapso_voice_agent.cloud: it checks the call with Kapso, then answers it with the same
code as `voice-agent serve`. See examples/pipecat-cloud/README.md.
"""

from pipecat.runner.types import RunnerArguments

from kapso_voice_agent.cloud import run


async def bot(runner_args: RunnerArguments):
    await run(runner_args.body)
