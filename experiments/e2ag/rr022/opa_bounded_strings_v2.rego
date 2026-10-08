package rr022.call_context

import rego.v1

# Additive v2 module, loaded WITH the frozen v1 call-context module.
# The version guard prevents the added spec from widening v1 semantics.
valid_constraints if {
    p := input.grant.argument_constraints
    is_object(p)
    object.keys(p) == {"version", "tools"}
    p.version == 2
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
    input.grant.argument_constraints.version == 2
    spec.type == "string"
    object.keys(spec) == {"type", "minLength", "maxLength"}
    is_number(spec.minLength)
    is_number(spec.maxLength)
    floor(spec.minLength) == spec.minLength
    floor(spec.maxLength) == spec.maxLength
    0 <= spec.minLength
    spec.minLength <= spec.maxLength
    spec.maxLength <= 262144
}

value_allowed(value, kind, spec) if {
    input.grant.argument_constraints.version == 2
    spec.type == "string"
    object.keys(spec) == {"type", "minLength", "maxLength"}
    kind == "str"
    is_string(value)
    spec.minLength <= count(value)
    count(value) <= spec.maxLength
}
