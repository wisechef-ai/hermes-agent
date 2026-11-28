"""A pinned job whose primary endpoint is down fails over to its own chain after <=2 attempts.

Regression for chef loopskill/cron-watchdog (1ad829e8696c): pinned to a GPU-time-shared local-llm
endpoint, it rode the full 10-attempt retry ladder (45-85 s backoffs) on every ``APIConnectionError``
and then raised, because a pinned job had no fallback chain. With the job's own declared chain
(``cron.scheduler._job_fallback_chain``) the conversation loop must switch to it after at most two
failed primary attempts, not wait out ``_api_max_retries``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai

from cron.scheduler import _job_fallback_chain
from run_agent import AIAgent

PRIMARY = ("local-llm", "local-llm")
FALLBACK = ("openai-codex", "gpt-5.6-sol")
JOB = {"id": "down-job", "provider": "local-llm", "model": "local-llm",
       "fallback_providers": [{"provider": FALLBACK[0], "model": FALLBACK[1]}]}


def _tool_defs():
    return [{"type": "function", "function": {
        "name": "web_search", "description": "search",
        "parameters": {"type": "object", "properties": {}}}}]


def _agent(chain):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k", base_url="http://100.106.27.136:8000/v1", provider=PRIMARY[0],
            model=PRIMARY[1], quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=chain,
        )
        agent.client = MagicMock()
        return agent


def _response(text):
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model=FALLBACK[1], usage=None)


def _drive(agent):
    """Run one turn; the primary always raises ``Connection error.``, the fallback answers."""
    calls = []
    request = httpx.Request("POST", "http://100.106.27.136:8000/v1/chat/completions")

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if (agent.provider, agent.model) == PRIMARY:
            raise openai.APIConnectionError(request=request)
        agent.api_mode = "chat_completions"  # the canned reply below is chat-completions shaped
        return _response("served by fallback")

    fb_client = MagicMock()
    fb_client.api_key, fb_client.base_url = "k", "https://chatgpt.com/backend-api/codex"
    fb_client._custom_headers = fb_client.default_headers = None
    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=lambda *a, **k: None),
        patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=lambda *a, **k: None),
        patch("agent.auxiliary_client.resolve_provider_client",
              return_value=(fb_client, FALLBACK[1])),
        patch("hermes_cli.model_normalize.normalize_model_for_provider",
              side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")
    return result, calls


def test_connection_refused_on_a_pinned_job_fails_over_within_two_primary_attempts():
    chain = _job_fallback_chain(JOB, {"fallback_providers": [{"provider": "zai", "model": "glm-5.3"}]})
    assert chain == [{"provider": FALLBACK[0], "model": FALLBACK[1]}]  # the job's own, never zai
    agent = _agent(chain)
    agent._api_max_retries = 10  # the production ladder; the loop must not ride it
    result, calls = _drive(agent)
    assert result["final_response"] == "served by fallback"
    assert calls[-1] == FALLBACK
    assert calls.count(PRIMARY) <= 2, calls


def test_without_a_declared_chain_a_pinned_job_burns_every_retry_on_the_dead_lane():
    """Control: the pre-fix behaviour. No chain to switch to, so the ladder (and the auto-recovery
    cycles after it) all land on the dead endpoint and the turn fails."""
    agent = _agent(_job_fallback_chain({**JOB, "fallback_providers": None}, {}))
    agent._api_max_retries = 4
    result, calls = _drive(agent)
    assert set(calls) == {PRIMARY} and len(calls) > 2
    assert not result.get("completed")
