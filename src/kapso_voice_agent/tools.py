"""Client tools the ElevenLabs agent can call. The schemas below are the single source of truth.

To add a tool: define an Arguments model, add a TOOLS entry, handle it in ToolRunner.run, and add
a test. `voice-agent tools schema` prints what the provider will receive.

The caller identity is fixed by the bridge for each call. No tool accepts a phone number, user ID
or other identifier for "whose" data to read, so a caller cannot ask for someone else's bookings.
"""

from collections import OrderedDict
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

MAX_REMEMBERED_RESULTS = 100


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InfoArgs(Arguments):
    topic: Literal["services", "hours", "closures", "location", "policies", "all"] = Field(
        default="all", description="Which business facts to read. services includes each service_id. all for an overview.")


class SlotsArgs(Arguments):
    day: str = Field(default="", max_length=10, description="Local date as YYYY-MM-DD, worked out from today's date "
                                                            "(for example for 'tomorrow' or 'next Tuesday'). "
                                                            "Empty for the soonest open times.")
    after: str = Field(default="", max_length=40, description="Exact slot value of the last time already offered, "
                                                              "to get later times. Empty otherwise.")


class BookArgs(Arguments):
    slot: str = Field(min_length=1, max_length=40, description="Exact slot value copied from an available_slots result.")
    name: str = Field(min_length=1, max_length=60, description="The caller's first name, as they said it.")
    service_id: str = Field(min_length=1, max_length=40, description="Exact service_id from business_info, not the service name.")
    confirmed: StrictBool = Field(description="true only after the caller clearly said yes to your read-back of "
                                              "service, day, time and name. Otherwise false.")
    note: str = Field(default="", max_length=200, description="Optional short description of the problem, in the "
                                                             "caller's words. No phone numbers or other personal details.")


class CancelArgs(Arguments):
    booking_id: str = Field(min_length=1, max_length=20, description="Booking id from my_appointments for this caller.")
    confirmed: StrictBool = Field(description="true only after the caller clearly said yes to cancelling this appointment. "
                                              "Otherwise false.")


TOOLS = {
    "business_info": (InfoArgs, "Read the business's services (with service_id), hours, closures, location or "
                                "policies. Call it before stating any business fact or choosing a service_id."),
    "available_slots": (SlotsArgs, "Find open appointment times. Returns up to two slots, each with an exact slot "
                                   "value for booking and a when phrase to say aloud; for a closed or full day, the "
                                   "reason and the next open day. Call it before offering any time."),
    "my_appointments": (Arguments, "List the current caller's upcoming appointments, with booking ids and when "
                                   "phrases. Takes no arguments: the call itself identifies the caller."),
    "book_appointment": (BookArgs, "Book an appointment for the current caller. Call it only after the caller "
                                   "clearly said yes to your read-back. Only ok=true means it is booked."),
    "cancel_appointment": (CancelArgs, "Cancel one of the current caller's appointments. Call it only after the "
                                       "caller clearly said yes. Only ok=true means it is cancelled."),
}

# Tool timing. pre_tool_speech "off" and no tool_call_sound: no "one second" line and no typing
# sound before a lookup. These tools answer locally in milliseconds.
TOOL_BEHAVIOR = {"expects_response": True, "response_timeout_secs": 10,
                 "pre_tool_speech": "off", "execution_mode": "immediate"}


def provider_tool_definitions():
    result = []
    for name, (model, description) in TOOLS.items():
        schema = model.model_json_schema()
        properties = {key: {k: v for k, v in value.items() if k in ("type", "description", "enum")}
                      for key, value in schema.get("properties", {}).items()}
        result.append({"type": "client", "name": name, "description": description,
                       "parameters": {"type": "object", "properties": properties,
                                      "required": schema.get("required", [])},
                       **TOOL_BEHAVIOR})
    return result


class ToolRunner:
    """Runs one call's tools. Results are remembered by tool_call_id so a retried call never double-books."""

    def __init__(self, store, caller):
        if not caller:
            raise ValueError("A caller identity is required")
        self.store, self.caller = store, caller
        self.results = OrderedDict()

    def execute(self, call):
        tool_id = str(call.get("tool_call_id", ""))[:128]
        if tool_id and tool_id in self.results:
            return self.results[tool_id]
        result = self.run(call.get("tool_name"), call.get("parameters") or {})
        response = {"type": "client_tool_result", "tool_call_id": tool_id,
                    "result": json.dumps(result), "is_error": not result.get("ok", False)}
        if tool_id:
            self.results[tool_id] = response
            if len(self.results) > MAX_REMEMBERED_RESULTS:
                self.results.popitem(last=False)
        return response

    def run(self, name, parameters):
        if name not in TOOLS:
            return {"ok": False, "code": "unknown_tool", "message": "That tool does not exist."}
        try:
            args = TOOLS[name][0].model_validate(parameters)
        except ValidationError as error:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in error.errors()})
            return {"ok": False, "code": "invalid_arguments",
                    "message": "Missing or invalid: " + ", ".join(fields) + ". Ask the caller for that detail."}
        try:
            match name:
                case "business_info":
                    return self.store.info(args.topic)
                case "available_slots":
                    return self.store.availability(args.day, args.after)
                case "my_appointments":
                    return self.store.list_own(self.caller)
                case "book_appointment":
                    return self.store.book(self.caller, args.slot, args.name, args.service_id, args.confirmed, args.note)
                case "cancel_appointment":
                    return self.store.cancel(self.caller, args.booking_id, args.confirmed)
        except Exception:  # noqa: BLE001 - a tool failure must reach the agent as data, not crash the call
            return {"ok": False, "code": "internal_error", "message": "The booking system had a problem. Say so; do not guess."}
