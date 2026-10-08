"""Turn agent/agent.toml + prompt.md + tool schemas into an ElevenLabs Agents config (no credentials)."""

from dataclasses import dataclass
from pathlib import Path
import tomllib

from .tools import provider_tool_definitions


@dataclass(frozen=True)
class AgentSpec:
    raw: dict
    directory: Path

    @property
    def name(self):
        return self.raw["agent"]["name"]

    @property
    def prompt(self):
        return (self.directory / self.raw["agent"]["prompt_file"]).read_text()

    @property
    def business_path(self):
        return self.directory / self.raw["agent"]["business_file"]

    @property
    def record_audio(self):
        return bool(self.raw["privacy"]["record_audio"])

    @property
    def max_duration_seconds(self):
        return int(self.raw["limits"]["max_duration_seconds"])

    def greeting(self, direction, recording):
        """direction: inbound|outbound. recording: whether any audio recording applies to this call."""
        greetings = self.raw["greetings"]
        notice = " " + greetings["recording_notice"] if recording else ""
        return greetings[direction].format(assistant=self.raw["agent"]["assistant_name"],
                                           business=self.raw["agent"]["business_name"], recording=notice)

    def recording_status(self, recording):
        if recording:
            return "This call's audio is recorded and kept privately for quality review."
        return "This call's audio is not recorded."


REQUIRED = {
    "agent": ["name", "assistant_name", "business_name", "language", "prompt_file", "business_file", "llm",
              "temperature", "max_tokens"],
    "voice": ["voice_id", "tts_model", "speed", "stability", "expressive_mode"],
    "asr": ["provider"],
    "turn": ["turn_timeout", "silence_end_call_timeout", "turn_eagerness"],
    "limits": ["max_duration_seconds", "concurrency_limit", "daily_limit"],
    "privacy": ["record_audio", "retention_days"],
    "greetings": ["inbound", "outbound", "recording_notice"],
}
# Words that make the agent narrate tool use or fill silence. The starter keeps them out of
# greetings and the prompt's instructions; `voice-agent check` enforces it.
FILLER_PHRASES = ("one second", "one moment", "hold on", "bear with me", "just a sec")


def load_spec(path):
    path = Path(path)
    raw = tomllib.loads(path.read_text())
    missing = [f"{section}.{key}" for section, keys in REQUIRED.items() for key in keys
               if key not in raw.get(section, {})]
    if missing:
        raise ValueError("agent.toml is missing: " + ", ".join(missing))
    spec = AgentSpec(raw, path.parent)
    if not spec.prompt.strip():
        raise ValueError("The prompt file is empty")
    for direction in ("inbound", "outbound"):
        for recording in (True, False):
            text = spec.greeting(direction, recording)
            if len(text) > 300:
                raise ValueError(f"The {direction} greeting is too long for a phone opening")
            if "AI" not in text:
                raise ValueError(f"The {direction} greeting must say the caller is talking to an AI")
            if any(phrase in text.lower() for phrase in FILLER_PHRASES):
                raise ValueError(f"The {direction} greeting contains filler")
    return spec


def build_config(spec, timezone):
    raw = spec.raw
    agent, voice, turn, limits, privacy = raw["agent"], raw["voice"], raw["turn"], raw["limits"], raw["privacy"]
    return {
        "name": agent["name"],
        "tags": list(agent.get("tags", [])),
        "conversation_config": {
            "agent": {
                # The bridge sends the greeting per call, so the recording sentence matches reality.
                "first_message": "{{opening_message}}",
                "language": agent["language"],
                # The AI (and recording) disclosure always plays in full.
                "disable_first_message_interruptions": True,
                "dynamic_variables": {"dynamic_variable_placeholders": {
                    "today": "2026-01-01", "timezone": timezone, "call_purpose": "inbound",
                    "opening_message": spec.greeting("inbound", spec.record_audio),
                    "recording_status": spec.recording_status(spec.record_audio)}},
                "prompt": {
                    "prompt": spec.prompt,
                    "llm": agent["llm"], "temperature": agent["temperature"], "max_tokens": agent["max_tokens"],
                    "timezone": timezone, "enable_parallel_tool_calls": False,
                    "tools": [*provider_tool_definitions(), {
                        "type": "system", "name": "end_call",
                        "description": "End the call when the caller says goodbye, asks to hang up, or confirms "
                                       "they need nothing else. Say one short goodbye first.",
                        # Sent explicitly: an omitted value is stored as pre_tool_speech "auto"
                        # (provider-chosen speech before the tool) and verify reports drift.
                        "pre_tool_speech": "off", "response_timeout_secs": 20,
                        "params": {"system_tool_type": "end_call"}}],
                },
            },
            "asr": {"provider": raw["asr"]["provider"], "user_input_audio_format": "pcm_16000",
                    "keywords": list(raw["asr"].get("keywords", []))},
            "tts": {"voice_id": voice["voice_id"], "model_id": voice["tts_model"],
                    "expressive_mode": bool(voice["expressive_mode"]),
                    "agent_output_audio_format": "pcm_16000", "stability": voice["stability"],
                    "speed": voice["speed"]},
            "turn": {"turn_timeout": turn["turn_timeout"], "silence_end_call_timeout": turn["silence_end_call_timeout"],
                     "turn_eagerness": turn["turn_eagerness"],
                     # -1 disables the provider's "Hmm..." filler while the LLM is thinking.
                     "soft_timeout_config": {"timeout_seconds": -1}},
            "conversation": {
                "max_duration_seconds": limits["max_duration_seconds"],
                "client_events": ["audio", "interruption", "user_transcript", "agent_response",
                                  "client_tool_call", "client_error"],
                # No background_sound: no fake office ambience under the voice.
            },
        },
        "platform_settings": {
            # Conversations need a server-issued signed URL; the API key never reaches a browser.
            "auth": {"enable_auth": True},
            "call_limits": {"agent_concurrency_limit": limits["concurrency_limit"],
                            "daily_limit": limits["daily_limit"], "bursting_enabled": False},
            "privacy": {"record_voice": bool(privacy["record_audio"]), "retention_days": privacy["retention_days"],
                        "delete_transcript_and_pii": False, "delete_audio": False, "zero_retention_mode": False},
        },
    }


# Read-back comparison --------------------------------------------------------------------------
#
# `agent verify` and `agent apply` compare what the provider stored with what this repository
# would send. Every value we send must come back equal (prompt text, voice, limits, privacy...).
# Fields the provider adds on its own (IDs, defaults we never set) are ignored, except where an
# addition changes behavior: an extra or missing tool, an extra tool parameter, a tool sound or
# background sound.

MISSING = object()
# Tool fields that define what a tool does and how the agent waits for it.
TOOL_FIELDS = ("type", "description", "expects_response", "response_timeout_secs", "pre_tool_speech",
               "execution_mode", "tool_call_sound")
PARAMETER_FIELDS = ("type", "description", "enum")
NOT_COMPARED = {"tags"}  # labels only; they do not change what the agent says or does


def _text(value):
    """Provider round trips may change line endings or trailing whitespace; nothing else."""
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip() if isinstance(value, str) else value


def tool_contract(tool):
    """The parts of one tool definition that change agent behavior, in a stable comparable form."""
    if not isinstance(tool, dict):
        return {"invalid": True}
    contract = {key: tool.get(key) for key in TOOL_FIELDS}
    contract["tool_call_sound"] = tool.get("tool_call_sound") or None  # "", null and absent all mean no sound
    contract["description"] = _text(contract["description"])
    if tool.get("type") == "system":
        contract["system_tool_type"] = (tool.get("params") or {}).get("system_tool_type")
    parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    contract["parameters"] = {
        "type": parameters.get("type"),
        "required": sorted(parameters.get("required") or []),
        "properties": {name: {key: _text(field.get(key)) for key in PARAMETER_FIELDS if field.get(key) is not None}
                       if isinstance(field, dict) else {"invalid": True} for name, field in properties.items()},
    }
    return contract


def _dig(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def tool_contracts(config):
    tools = _dig(config, "conversation_config", "agent", "prompt", "tools")
    contracts = {}
    for tool in tools if isinstance(tools, list) else []:
        name = tool.get("name") if isinstance(tool, dict) else None
        contracts[name if name not in contracts else f"{name} (duplicate)"] = tool_contract(tool)
    return contracts


def _drift(expected, actual, path, found):
    """Report every value in `expected` that `actual` does not hold. Extra keys in `actual` are ignored."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            found.append(path)
            return
        for key, value in expected.items():
            if not path and key in NOT_COMPARED:
                continue
            _drift(value, actual.get(key, MISSING), f"{path}.{key}" if path else key, found)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            found.append(path)
            return
        for index, (want, have) in enumerate(zip(expected, actual, strict=True)):
            _drift(want, have, f"{path}[{index}]", found)
    elif _text(expected) != _text(actual) or isinstance(expected, bool) != isinstance(actual, bool):
        found.append(path)


def mismatches(expected_config, actual):
    """Dotted paths where the stored agent differs from what this repository would send."""
    found = []
    expected_prompt = expected_config["conversation_config"]["agent"]["prompt"]
    without_tools = {**expected_config, "conversation_config": {
        **expected_config["conversation_config"], "agent": {
            **expected_config["conversation_config"]["agent"],
            "prompt": {k: v for k, v in expected_prompt.items() if k != "tools"}}}}
    _drift(without_tools, actual, "", found)
    want, have = tool_contracts(expected_config), tool_contracts(actual)
    for name in sorted(set(want) | set(have), key=str):
        if want.get(name) != have.get(name):
            if name not in want or name not in have:
                found.append(f"tools[{name}]")
                continue
            for key in want[name]:
                if want[name][key] != have[name].get(key):
                    found.append(f"tools[{name}].{key}")
    if _dig(actual, "conversation_config", "conversation", "background_sound", "source_id"):
        found.append("conversation_config.conversation.background_sound")
    return sorted(set(found))
