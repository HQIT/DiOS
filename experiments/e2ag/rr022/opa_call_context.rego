package rr022.call_context

import rego.v1

# Call-boundary PDP only. The host authenticates credentials, owns state and
# enforces decisions. Event admission / approval state transitions are NOT here.
default grant_reason := "OPA_CONTEXT_INVALID"

grant_reason := "TOOL_GRANT_INVALID" if {
    input.authenticated == false
} else := "TOOL_GRANT_NOT_ACTIVE" if {
    input.grant.status != "active"
} else := "TOOL_GRANT_EXPIRED" if {
    input.grant.expires_at <= input.now
} else := "TOOL_GRANT_TASK_MISMATCH" if {
    not binding_matches
} else := "TOOL_GRANT_TASK_TERMINAL" if {
    input.task.status in {"completed", "failed", "canceled"}
} else := "TOOL_GRANT_VALID" if {
    input.authenticated == true
    input.grant.status == "active"
    is_number(input.now)
    is_number(input.grant.expires_at)
    input.now < input.grant.expires_at
    binding_matches
    is_string(input.task.status)
}

binding_matches if {
    is_object(input.task)
    input.task.id == input.grant.task_id
    input.task.trace_id == input.grant.trace_id
    input.task.agent_id == input.grant.agent_id
}

default granted_tool := false
granted_tool if {
    some pattern in input.grant.allowed_tools
    glob.match(pattern, [], input.call.name)
}

default valid_constraints := false
valid_constraints if {
    p := input.grant.argument_constraints
    is_object(p)
    object.keys(p) == {"version", "tools"}
    p.version == 1
    is_object(p.tools)
    count(p.tools) > 0
    every tool, fields in p.tools {
        is_string(tool)
        tool != ""
        not regex.match("[*?\\[]", tool)
        is_object(fields)
        every field, spec in fields {
            field != ""
            valid_spec(spec)
        }
    }
}

valid_spec(spec) if {
    object.keys(spec) == {"type", "enum"}
    spec.type == "string"
    is_array(spec.enum)
    count(spec.enum) > 0
    every value in spec.enum { is_string(value) }
}
valid_spec(spec) if {
    object.keys(spec) == {"type", "minimum", "maximum"}
    spec.type == "integer"
    is_number(spec.minimum)
    is_number(spec.maximum)
    floor(spec.minimum) == spec.minimum
    floor(spec.maximum) == spec.maximum
    spec.minimum <= spec.maximum
}

default arguments_allowed := false
arguments_allowed if {
    fields := input.grant.argument_constraints.tools[input.call.name]
    is_object(input.call.arguments)
    object.keys(input.call.arguments) == object.keys(fields)
    every field, spec in fields {
        value_allowed(input.call.arguments[field], input.argument_types[field], spec)
    }
}

value_allowed(value, kind, spec) if {
    spec.type == "string"
    kind == "str"
    is_string(value)
    value in spec.enum
}
value_allowed(value, kind, spec) if {
    spec.type == "integer"
    # Host parser type fact, NOT a permission verdict. Rego numbers otherwise
    # collapse the distinction between JSON 10 and 10.0 used by this protocol.
    kind == "int"
    is_number(value)
    floor(value) == value
    value >= spec.minimum
    value <= spec.maximum
}

default call_reason := "OPA_CALL_CONTEXT_INVALID"
call_reason := grant_reason if {
    grant_reason != "TOOL_GRANT_VALID"
} else := "MCP_CONTROL_METHOD_ALLOWED" if {
    input.call.method in {"initialize", "notifications/initialized", "ping", "tools/list"}
} else := "MCP_METHOD_NOT_ALLOWED" if {
    input.call.method != "tools/call"
} else := "MCP_TOOL_NOT_GRANTED" if {
    not granted_tool
} else := "MCP_TOOL_GRANTED" if {
    input.grant.argument_constraints == null
} else := "MCP_ARGUMENT_CONSTRAINT_INVALID" if {
    not valid_constraints
} else := "MCP_ARGUMENT_TOOL_UNSPECIFIED" if {
    not input.grant.argument_constraints.tools[input.call.name]
} else := "MCP_ARGUMENTS_GRANTED" if {
    arguments_allowed
} else := "MCP_ARGUMENTS_DENIED" if {
    not arguments_allowed
}

decision := {"allow": grant_reason == "TOOL_GRANT_VALID", "reason": grant_reason} if {
    input.phase == "grant"
}
decision := {"allow": call_reason in {"MCP_CONTROL_METHOD_ALLOWED", "MCP_TOOL_GRANTED", "MCP_ARGUMENTS_GRANTED"}, "reason": call_reason} if {
    input.phase == "call"
}
