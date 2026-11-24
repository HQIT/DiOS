package dios.delegation

import rego.v1

# Trusted OPA data, written by the authority publisher; never copied from input.
# Chain index 0 is the root authority, followed by at most three delegations.
default allow := false

# Runtime supplies this snapshot from its current authorization transaction.
# The Data API variant remains available for isolated Provider qualification.
authority := input.context.delegation if { "delegation" in object.keys(input.context) }
authority := data.dios_delegation if { not "delegation" in object.keys(input.context) }

valid_permission(p) if {
    object.keys(p) == {"action", "resource_kind", "resource_id", "resource_version", "constraint_revision"}
    every _, v in p { is_string(v); count(v) > 0 }
}

valid_parent(link, chain, i) if {
    i == 0
    link.parent_ref == null
}

valid_parent(link, chain, i) if {
    i > 0
    link.parent_ref == chain[i - 1]
    parent := authority.links[chain[i - 1]]
    time.parse_rfc3339_ns(link.expires_at) <= time.parse_rfc3339_ns(parent.expires_at)
    every p in link.permissions { p in parent.permissions }
}

allow if {
    binding := input.context.binding
    receipt := authority.work_bindings[binding.authorization_ref]
    receipt.scope == input.context.scope
    receipt.subject_ref == binding.subject
    receipt.actor_ref == binding.actor
    receipt.run_id == binding.run_id
    chain := receipt.chain
    is_array(chain)
    count(chain) >= 1
    count(chain) <= 4
    count({id | id := chain[_]}) == count(chain)
    object.keys(receipt.epochs) == {id | id := chain[_]}
    leaf := authority.links[chain[count(chain) - 1]]
    leaf.actor_ref == binding.actor
    input.context.permission in leaf.permissions
    now := time.now_ns()
    every i, id in chain {
        link := authority.links[id]
        link.id == id
        link.depth == i
        link.state == "active"
        link.scope == input.context.scope
        link.subject_ref == binding.subject
        link.audience == "dios-runtime"
        is_string(link.actor_ref)
        count(link.actor_ref) > 0
        link.authorization_epoch > 0
        link.authorization_epoch == floor(link.authorization_epoch)
        link.authorization_epoch == receipt.epochs[id]
        time.parse_rfc3339_ns(link.expires_at) > now
        is_array(link.permissions)
        count(link.permissions) > 0
        count({p | p := link.permissions[_]}) == count(link.permissions)
        every p in link.permissions { valid_permission(p) }
        valid_parent(link, chain, i)
    }
}
