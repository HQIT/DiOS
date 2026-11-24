package dios.tools
import rego.v1

default allow := false

# The host supplies trusted permissions and binding; the model supplies arguments.
# This sample narrows search text length in addition to the complete tuple check.
allow if {
    every layer in ["caller", "service", "workload", "resource"] {
        input.context.permission in input.context.permissions[layer]
    }
    is_string(input.action.properties.arguments.query)
    count(input.action.properties.arguments.query) <= 2048
}
