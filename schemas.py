"""Tool schema for the delegate-agy plugin."""

from __future__ import annotations

_TASK_PROPERTIES = {
    "goal": {
        "type": "string",
        "description": "Self-contained task for the Antigravity worker.",
    },
    "context": {
        "type": "string",
        "description": "Optional background, constraints, paths, and expected output.",
    },
    "task_type": {
        "type": "string",
        "enum": [
            "simple",
            "investigation",
            "implementation",
            "hard",
            "review",
            "exceptional",
            "cheap_second_opinion",
        ],
        "description": (
            "Routing class. simple uses gemini-3.7-flash-low; investigation uses "
            "gemini-3.7-flash-medium; implementation uses gemini-3.7-flash-high; "
            "hard uses gemini-3.1-pro-high; review uses claude-sonnet-4-6; "
            "exceptional uses claude-opus-4-6-thinking; cheap_second_opinion uses "
            "gpt-oss-120b-medium. Defaults to investigation when model is omitted."
        ),
    },
    "model": {
        "type": "string",
        "description": "Optional explicit agy model ID. Overrides task_type routing.",
    },
    "execution_mode": {
        "type": "string",
        "enum": ["sandbox", "accept_edits"],
        "description": (
            "sandbox runs in plan mode with terminal sandboxing and restricted permissions "
            "(default). Use accept_edits only when file modifications are explicitly required; "
            "it requires allow_accept_edits: true in plugin settings."
        ),
    },
    "effort": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "description": "Optional agy effort level.",
    },
    "workdir": {
        "type": "string",
        "description": (
            "Existing local directory where agy runs. Always pass this for code tasks."
        ),
    },
    "timeout_seconds": {
        "type": "integer",
        "minimum": 30,
        "maximum": 7200,
        "description": "Bound for the agy print run. Defaults to 600 seconds.",
    },
}

DELEGATE_AGY_SCHEMA = {
    "name": "delegate_agy",
    "description": (
        "Delegate work to Antigravity CLI instead of spending the main Hermes model on "
        "the whole task. Use action=spawn for one goal or a parallel tasks batch. The "
        "main agent should select the cheapest adequate task_type: simple for reading, "
        "lookup, extraction, or classification; investigation for normal diagnosis; "
        "implementation for routine code changes; hard for difficult reasoning or large "
        "context; review for an independent Claude review; exceptional only for unusually "
        "hard work; cheap_second_opinion for a low-cost cross-check. Spawn is asynchronous "
        "unless wait=true. Use list/status/result/wait to collect workers, stop to cancel, "
        "continue to resume a completed conversation, and steer to queue a follow-up in the "
        "same conversation after the current print run finishes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "spawn",
                    "list",
                    "status",
                    "result",
                    "wait",
                    "stop",
                    "steer",
                    "continue",
                ],
                "description": "Operation to perform. Defaults to spawn.",
            },
            **_TASK_PROPERTIES,
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "description": "Independent tasks to launch in parallel.",
                "items": {
                    "type": "object",
                    "properties": _TASK_PROPERTIES,
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "For spawn, wait for all workers and return their results in this call. "
                    "Defaults to false."
                ),
            },
            "worker_id": {
                "type": "string",
                "description": "Target worker for status, result, stop, steer, continue, or wait.",
            },
            "worker_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Targets for wait. Omit to wait for all workers owned by this session.",
            },
            "message": {
                "type": "string",
                "description": "Follow-up instruction for steer or continue.",
            },
            "wait_timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 7200,
                "description": "Maximum time for action=wait or spawn with wait=true.",
            },
        },
        "additionalProperties": False,
    },
}
