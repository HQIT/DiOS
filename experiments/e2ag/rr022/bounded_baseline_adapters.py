"""Baseline-only implementations. Does not import the E2AG argument predicate."""
import copy
from run_opa_audited_pilot import AuditedOpaAdapter
from run_opa_context_pilot import wsl_path
from run_paired_tool_pilot import HERE


class BoundedOpaAdapter(AuditedOpaAdapter):
    def __init__(self):
        super().__init__()
        index = self.command.index('--stdin-input')
        self.command[index:index] = ['--data', wsl_path(HERE / 'opa_bounded_strings_v2.rego')]

    def authorize(self, method, params, allowed_tools, argument_constraints=None):
        if self.context is None:
            raise RuntimeError('No authenticated OPA context')
        ctx = copy.deepcopy(self.context)
        assert ctx['grant']['allowed_tools'] == allowed_tools
        assert ctx['grant']['argument_constraints'] == argument_constraints
        arguments = params.get('arguments')
        types = {}
        if isinstance(arguments, dict):
            for key, value in arguments.items():
                types[key] = type(value).__name__
                if isinstance(value, str):
                    try:
                        value.encode('utf-8', errors='strict')
                    except UnicodeEncodeError:
                        types[key] = 'invalid_unicode'
        # Parser facts only. Rego independently computes length/range/identity.
        ctx.update(phase='call', call={'method': method, 'name': params.get('name'), 'arguments': arguments},
                   argument_types=types if isinstance(arguments, dict) else None)
        result = self.evaluate(ctx)
        return result['allow'], result['reason']


def agentspec_arguments_allowed(fields, arguments):
    """Independent custom-predicate logic for prevalidated frozen v2 policies."""
    if not isinstance(arguments, dict) or set(fields) != set(arguments):
        return False
    for name, constraint in fields.items():
        value = arguments[name]
        if constraint['type'] == 'integer':
            if type(value) is not int or value < constraint['minimum'] or value > constraint['maximum']:
                return False
        elif constraint['type'] == 'string':
            if type(value) is not str:
                return False
            if 'enum' in constraint:
                if value not in constraint['enum']:
                    return False
            else:
                try:
                    value.encode('utf-8', errors='strict')
                except UnicodeEncodeError:
                    return False
                # Count Unicode code points, not UTF-8 bytes or grapheme clusters.
                count = sum(1 for _ in value)
                if count < constraint['minLength'] or count > constraint['maxLength']:
                    return False
        else:
            return False
    return True
