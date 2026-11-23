"""Data-only input registration. Application code never runs in the API."""
import hashlib
import json
import re
from pathlib import Path
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, SchemaError
from referencing.exceptions import Unresolvable
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest, encoded

CONSOLE_ORIGIN = 'task-console/v1'
# Read compatibility only; new admissions and kinds come exclusively from profiles.
LEGACY_INPUT_KINDS = ('inline-static-review', 'knowledge-evidence')


def is_console_input(value):
    return (value.get('origin') == CONSOLE_ORIGIN or
            (value.get('origin') is None and value.get('task_kind') in LEGACY_INPUT_KINDS))


def _local_schema(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'$ref', '$dynamicRef'} and (not isinstance(item, str) or not item.startswith('#')):
                raise ValueError('Only local schema references are supported')
            if key == '$id':
                raise ValueError('Schema base URI changes are not supported')
            _local_schema(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema(item)


def registration(display):
    """Read one operator-controlled catalog, validate and fingerprint its contract."""
    try:
        kind = display['input_kind']
        if not isinstance(kind, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,159}', kind):
            raise ValueError()
        path = Path(display['input_catalog'])
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError()
        if set(value) - {'version', 'inputs', 'schema', 'input_digest', 'requires_e2ag'}:
            raise ValueError()
        rows = value['inputs']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
            raise ValueError()
        schema = value.get('schema', {'type': 'object'})
        _local_schema(schema)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        strategy = value.get('input_digest', 'canonical-sha256')
        if strategy not in {'canonical-sha256', 'source-sha256'}:
            raise ValueError()
        governed = value.get('requires_e2ag', True)
        if type(governed) is not bool:
            raise ValueError()
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or len(encoded(row)) > 12288:
                raise ValueError()
            for field in ('id', 'name', 'sha256'):
                if not isinstance(row.get(field), str) or not 1 <= len(row[field]) <= 160:
                    raise ValueError()
            if row['id'] in seen:
                raise ValueError()
            seen.add(row['id'])
            validator.validate(row)
            expected = (hashlib.sha256(row['source'].encode('utf-8')).hexdigest()
                        if strategy == 'source-sha256'
                        else digest({k: v for k, v in row.items() if k != 'sha256'}))
            if row['sha256'] != expected:
                raise ValueError()
        return dict(kind=kind, inputs=rows, digest=digest(value), requires_e2ag=governed)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
            ValidationError, SchemaError, Unresolvable):
        raise RuntimeErrorCode('INVALID_INPUT_CATALOG') from None


def inputs(display):
    return registration(display)['inputs']
