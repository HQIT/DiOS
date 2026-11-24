package dios.delegated_tools

import rego.v1

default allow := false

# Keep the existing complete tuple/parameter gate and add the delegation gate.
# Register this endpoint for delegated tools; do not add a second allow branch
# to dios.tools, because separate allow rules are alternatives in Rego.
allow if {
    data.dios.tools.allow
    data.dios.delegation.allow
}
