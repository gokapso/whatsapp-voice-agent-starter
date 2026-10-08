import copy
import json
import os
from pathlib import Path
import re

import pytest

from kapso_voice_agent.agent_config import build_config, load_spec, mismatches
from kapso_voice_agent.config import REPO_ROOT
from kapso_voice_agent.provider import validate_against_openapi


@pytest.fixture
def config(spec):
    return build_config(spec, "America/Chicago")


class TestNoFillerOrPerformedSounds:
    def test_tools_have_no_pre_tool_speech_or_sounds(self, config):
        tools = config["conversation_config"]["agent"]["prompt"]["tools"]
        client = [t for t in tools if t["type"] == "client"]
        assert client and all(t["pre_tool_speech"] == "off" and t["execution_mode"] == "immediate" for t in client)
        # System tools too: the provider stores an omitted value as "auto".
        assert all(t["pre_tool_speech"] == "off" for t in tools)
        assert not any("tool_call_sound" in t or "tool_call_sound_behavior" in t for t in tools)

    def test_soft_timeout_filler_background_sound_and_expressive_tags_are_off(self, config):
        conversation = config["conversation_config"]
        assert conversation["turn"]["soft_timeout_config"] == {"timeout_seconds": -1}
        assert "background_sound" not in conversation["conversation"]
        assert conversation["tts"]["expressive_mode"] is False

    def test_prompt_forbids_tool_narration_instead_of_requiring_it(self, spec):
        prompt = " ".join(spec.prompt.lower().split())
        assert "typing sound" not in prompt
        assert 'do not say "one moment", "let me check"' in prompt
        # Every mention of a filler phrase is inside a prohibition.
        for phrase in ("one moment", "let me check", "one second"):
            for match in re.finditer(re.escape(phrase), prompt):
                assert "do not" in prompt[max(0, match.start() - 40):match.start()], phrase


class TestDisclosure:
    @pytest.mark.parametrize("direction", ["inbound", "outbound"])
    def test_greeting_states_ai_and_recording_only_when_recording_applies(self, spec, direction):
        on, off = spec.greeting(direction, True), spec.greeting(direction, False)
        assert "AI" in on and "AI" in off
        assert "recorded" in on and "recorded" not in off
        assert "test call" not in on.lower()

    def test_nothing_the_agent_reads_frames_the_call_as_a_test_or_demo(self, spec, config):
        spoken = [spec.prompt, spec.recording_status(True), spec.recording_status(False),
                  *(spec.greeting(d, r) for d in ("inbound", "outbound") for r in (True, False)),
                  json.dumps(config["conversation_config"]["agent"]["prompt"]["tools"])]
        framing = re.compile(r"\b(tests?|testing|demo|fictional|simulat\w*|fake)\b", re.IGNORECASE)
        assert [match.group(0) for text in spoken for match in framing.finditer(text)] == []

    def test_first_message_is_per_call_and_uninterruptible(self, config):
        agent = config["conversation_config"]["agent"]
        assert agent["first_message"] == "{{opening_message}}" and agent["disable_first_message_interruptions"]
        placeholders = agent["dynamic_variables"]["dynamic_variable_placeholders"]
        for name in re.findall(r"{{(\w+)}}", agent["prompt"]["prompt"]):
            assert name in placeholders, name

    def test_privacy_matches_spec(self, config, spec):
        privacy = config["platform_settings"]["privacy"]
        assert privacy["record_voice"] is spec.record_audio and privacy["retention_days"] == 7
        assert config["platform_settings"]["auth"]["enable_auth"] is True

    def test_spec_rejects_greeting_without_ai_disclosure(self, tmp_path):
        text = (REPO_ROOT / "agent/agent.toml").read_text().replace("the AI assistant", "the assistant")
        for name in ("prompt.md", "business.json"):
            (tmp_path / name).write_text((REPO_ROOT / "agent" / name).read_text())
        (tmp_path / "agent.toml").write_text(text)
        with pytest.raises(ValueError, match="AI"):
            load_spec(tmp_path / "agent.toml")


def test_tested_route_settings_are_kept(config):
    conversation = config["conversation_config"]
    assert conversation["agent"]["language"] == "en"
    assert conversation["tts"]["agent_output_audio_format"] == conversation["asr"]["user_input_audio_format"] == "pcm_16000"
    assert conversation["tts"]["model_id"] == "eleven_v4_turbo" and conversation["asr"]["provider"] == "scribe_realtime"
    assert config["platform_settings"]["call_limits"]["agent_concurrency_limit"] == 1
    assert conversation["conversation"]["max_duration_seconds"] == 300


# Defaults ElevenLabs stored for tool fields the request left out (real read-back of a new agent,
# 2026-10-07). A behavior field the repo omits comes back as one of these and fails verify.
PROVIDER_TOOL_DEFAULTS = {"pre_tool_speech": "auto", "force_pre_tool_speech": False, "response_timeout_secs": 20,
                          "tool_call_sound": None, "tool_call_sound_behavior": "auto", "disable_interruptions": False,
                          "interruption_mode": "allow", "tool_error_handling_mode": "auto", "assignments": []}


def provider_copy(config):
    """What a provider read-back might look like: same values plus fields the provider adds itself."""
    stored = copy.deepcopy(config)
    stored["agent_id"] = "synthetic-agent"
    prompt = stored["conversation_config"]["agent"]["prompt"]
    prompt["prompt"] = prompt["prompt"].replace("\n", "\r\n") + "\n"
    prompt["rag"] = {"enabled": False}
    for tool in prompt["tools"]:
        for key, value in PROVIDER_TOOL_DEFAULTS.items():
            tool.setdefault(key, copy.deepcopy(value))
        tool["id"] = "tool-" + tool["name"]
        tool["dynamic_variables"] = {"dynamic_variable_placeholders": {}}
        if tool["type"] == "client":
            tool["tool_call_sound"] = None
            tool["parameters"]["required"] = list(reversed(tool["parameters"]["required"]))
            for field in tool["parameters"]["properties"].values():
                field.setdefault("enum", None)
                field["dynamic_variable"] = ""
        else:
            tool["params"]["transfer_to_number"] = None
    stored["conversation_config"]["tts"]["optimize_streaming_latency"] = 3
    stored["tags"] = []
    return stored


def tool(stored, name):
    return next(t for t in stored["conversation_config"]["agent"]["prompt"]["tools"] if t["name"] == name)


class TestReadback:
    def test_identical_config_and_provider_added_fields_verify(self, config):
        assert mismatches(config, copy.deepcopy(config)) == []
        assert mismatches(config, provider_copy(config)) == []

    def test_settings_and_sound_drift(self, config):
        stored = provider_copy(config)
        stored["conversation_config"]["tts"]["voice_id"] = "other"
        tool(stored, "business_info")["tool_call_sound"] = "typing"
        stored["conversation_config"]["conversation"]["background_sound"] = {"source_id": "office1"}
        stored["platform_settings"]["privacy"]["record_voice"] = False
        assert mismatches(config, stored) == ["conversation_config.conversation.background_sound",
                                              "conversation_config.tts.voice_id",
                                              "platform_settings.privacy.record_voice",
                                              "tools[business_info].tool_call_sound"]

    def test_prompt_drift_is_reported(self, config):
        stored = provider_copy(config)
        stored["conversation_config"]["agent"]["prompt"]["prompt"] += "\nBefore every tool, say: one moment please."
        assert mismatches(config, stored) == ["conversation_config.agent.prompt.prompt"]

    @pytest.mark.parametrize("change,expected", [
        (lambda t: t["parameters"].update(required=["slot"]), "parameters"),
        (lambda t: t["parameters"]["properties"].pop("confirmed"), "parameters"),
        (lambda t: t["parameters"]["properties"].update(phone={"type": "string", "description": "Any number"}), "parameters"),
        (lambda t: t["parameters"]["properties"]["confirmed"].update(type="string"), "parameters"),
        (lambda t: t["parameters"]["properties"]["name"].update(description="Anything"), "parameters"),
        (lambda t: t.update(description="Book without asking."), "description"),
        (lambda t: t.update(expects_response=False), "expects_response"),
        (lambda t: t.update(response_timeout_secs=60), "response_timeout_secs"),
        (lambda t: t.update(execution_mode="async"), "execution_mode"),
        (lambda t: t.update(pre_tool_speech="force"), "pre_tool_speech"),
    ])
    def test_tool_schema_and_response_drift_is_reported(self, config, change, expected):
        stored = provider_copy(config)
        change(tool(stored, "book_appointment"))
        assert mismatches(config, stored) == [f"tools[book_appointment].{expected}"]

    def test_added_or_missing_tools_are_reported(self, config):
        stored = provider_copy(config)
        tools = stored["conversation_config"]["agent"]["prompt"]["tools"]
        tools.remove(tool(stored, "cancel_appointment"))
        tools.append({"type": "webhook", "name": "lookup_customer", "description": "Look up anyone"})
        assert mismatches(config, stored) == ["tools[cancel_appointment]", "tools[lookup_customer]"]

    def test_end_call_behavior_is_compared(self, config):
        stored = provider_copy(config)
        tool(stored, "end_call")["description"] = "Never end the call."
        assert mismatches(config, stored) == ["tools[end_call].description"]

    def test_unreadable_readback_never_verifies(self, config):
        assert "conversation_config" in mismatches(config, {"name": config["name"]})


@pytest.mark.skipif(not os.environ.get("ELEVENLABS_OPENAPI"), reason="set ELEVENLABS_OPENAPI to a downloaded spec file")
def test_config_matches_provider_openapi(config):
    document = json.loads(Path(os.environ["ELEVENLABS_OPENAPI"]).read_text())
    assert validate_against_openapi(config, document) == []


class TestOptInProviderTests:
    """agent/provider-tests holds ElevenLabs agent tests (the CLI's test_configs format). They are
    judged by the provider's LLM, so they only run when an operator uploads them (docs/testing.md).
    Here we check offline that they are well formed and describe calls this server can produce."""

    @pytest.fixture
    def provider_tests(self):
        paths = sorted((REPO_ROOT / "agent/provider-tests").glob("*.json"))
        assert paths
        return {path.name: json.loads(path.read_text()) for path in paths}

    def test_shape_matches_elevenlabs_llm_tests(self, provider_tests):
        names = [body["name"] for body in provider_tests.values()]
        assert len(set(names)) == len(names)
        for body in provider_tests.values():
            assert body["type"] == "llm" and body["success_condition"].strip()
            assert body["success_examples"] and all(e["type"] == "success" and e["response"] for e in body["success_examples"])
            assert body["failure_examples"] and all(e["type"] == "failure" and e["response"] for e in body["failure_examples"])
            history = body["chat_history"]
            assert {turn["role"] for turn in history} <= {"user", "agent"} and history[-1]["role"] == "user"
            times = [turn["time_in_call_secs"] for turn in history]
            assert all(isinstance(t, int) for t in times) and times == sorted(times)

    def test_variables_and_greeting_match_what_a_real_call_sends(self, provider_tests, spec):
        prompt_variables = set(re.findall(r"{{(\w+)}}", spec.prompt))
        notice = spec.raw["greetings"]["recording_notice"]
        for name, body in provider_tests.items():
            variables = body["dynamic_variables"]
            assert set(variables) == prompt_variables, name
            recorded = variables["recording_status"] == spec.recording_status(True)
            assert recorded or variables["recording_status"] == spec.recording_status(False), name
            greeting = body["chat_history"][0]
            assert greeting["role"] == "agent" and "AI" in greeting["message"], name
            assert (notice in greeting["message"]) is recorded, name
