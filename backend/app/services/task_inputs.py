"""Operator-registered application inputs; no client paths or executable commands."""
import json
from pathlib import Path
from app.apps.inline_review import sample,SAMPLES
from app.apps.evidence_brief import KIND,validate_input
from app.runtime.database import RuntimeErrorCode

KINDS=('inline-static-review',KIND)


def inputs(display):
    if display.get('input_kind')=='inline-static-review':return [sample(k) for k in SAMPLES]
    if display.get('input_kind')!=KIND:return []
    path=Path(display.get('input_catalog',''))
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.stat().st_size>65536:
        raise RuntimeErrorCode('INVALID_INPUT_CATALOG')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('inputs'),list) or not 1<=len(value['inputs'])<=20:
        raise RuntimeErrorCode('INVALID_INPUT_CATALOG')
    rows=[validate_input(x) for x in value['inputs']]
    if len({x['id'] for x in rows})!=len(rows):raise RuntimeErrorCode('INVALID_INPUT_CATALOG')
    return rows
