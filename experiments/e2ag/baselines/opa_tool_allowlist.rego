package e2ag.tool_boundary

default allow := false

allow if {
    input.method == "tools/call"
    some pattern in data.allowed_tools
    glob.match(pattern, null, input.tool)
}
