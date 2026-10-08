package rr022.admission
import rego.v1

# Registry declarations and target configuration are shared DATA, not decisions.
valid_event if {
    {"specversion", "id", "source", "type", "data"} - object.keys(input.event) == set()
    input.event.specversion == "1.0"
    is_object(input.event.data)
    every name in ["id", "source", "type"] {
        is_string(input.event[name])
        input.event[name] != ""
    }
}
binding if {
    valid_event
    some declared in input.bindings
    input.event.type == declared.event_type
    glob.match(declared.pattern, [], input.event.source)
}
default contract := {"decision": "deny", "reason": "CONTRACT_SOURCE_TYPE_UNBOUND"}
contract := {"decision": "allow", "reason": "CONTRACT_BOUND"} if { binding }

action := object.get(input.event.data, "requested_action", "")
tool := object.get(input.event.data, "requested_tool", "")

matches(value, patterns) if {
    some pattern in patterns
    glob.match(pattern, [], value)
}

violations contains "governance_missing" if {
    some _, caps in input.targets
    count(object.get(caps, "governance", {})) == 0
}
violations contains "source" if {
    some _, caps in input.targets
    patterns := object.get(caps.governance, "allowed_event_sources", [])
    count(patterns) > 0
    not matches(input.event.source, patterns)
}
violations contains "event_type" if {
    some _, caps in input.targets
    patterns := object.get(caps.governance, "allowed_event_types", [])
    count(patterns) > 0
    not matches(input.event.type, patterns)
}
violations contains "tool" if {
    some _, caps in input.targets
    patterns := object.get(caps.governance, "allowed_tools", [])
    tool != ""
    count(patterns) > 0
    not matches(tool, patterns)
}
violations contains "action" if {
    some _, caps in input.targets
    patterns := object.get(caps.governance, "allowed_actions", [])
    action != ""
    count(patterns) > 0
    not matches(action, patterns)
}
violations contains "action_missing" if {
    some _, caps in input.targets
    caps.governance.require_action_declaration == true
    action == ""
}
sensitive if {
    some prefix in input.sensitive_prefixes
    startswith(action, prefix)
}

policy := {"decision": "deny", "reason": "POLICY_CONTRACT_DENIED"} if {
    not binding
} else := {"decision": "deny", "reason": "POLICY_TARGET_CAPABILITY_VIOLATION"} if {
    count(violations) > 0
} else := {"decision": "approval_required", "reason": "POLICY_PRODUCTION_SENSITIVE_ACTION"} if {
    sensitive
    lower(object.get(input.event.data, "environment", "unknown")) in {"prod", "production"}
} else := {"decision": "allow", "reason": "POLICY_ALLOWED"}

result := {"contract": contract, "policy": policy}
