"""Backend support, independent of endpoint reachability and MCP policy."""


def capabilities(writes_enabled: bool) -> dict:
    names = ("read_inputs", "read_points", "control_binary_output", "poll_class", "list_points", "ping")
    return {
        "protocol": "dnp3",
        "backend": "dnp3py",
        "simulated": False,
        "scope": "backend_support",
        "device_verified": False,
        "operations": {
            name: {"supported": True, "enabled": writes_enabled if name == "control_binary_output" else True}
            for name in names
        },
        "prerequisites": [
            "Configured TCP outstation and matching link addresses",
            "Device must support the requested groups and variations",
        ],
        "limitations": [
            "TCP only; no serial or Secure Authentication implementation",
            "CROB latch controls only; no analog output controls",
            "No persistent unsolicited subscription; events are captured during calls",
            "enabled describes native configuration, not MCP policy or device permission",
        ],
    }
