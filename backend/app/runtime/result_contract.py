"""Data-only result schemas frozen at admission, enforced by the authoritative store."""
import json
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, SchemaError
from referencing.exceptions import Unresolvable
from .database import RuntimeErrorCode

def local_schema(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {'$ref','$dynamicRef'} and (not isinstance(item,str) or not item.startswith('#')):
                raise ValueError('External schema reference')
            if key=='$id': raise ValueError('Schema base URI changes are not supported')
            local_schema(item)
    elif isinstance(value,list):
        for item in value: local_schema(item)

def validate_result(input_record, value):
    schema=input_record.get('input_registration',{}).get('result_schema')
    if schema is None:return
    try:
        json.dumps(value,allow_nan=False)
        local_schema(schema)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except (ValidationError,SchemaError,Unresolvable,ValueError,TypeError,RecursionError):
        raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT') from None

def validate_bytes(input_record, data):
    if input_record.get('input_registration',{}).get('result_schema') is None:return
    try:
        if len(data)>65536:raise ValueError()
        value=json.loads(data.decode('utf-8'))
    except (ValueError,UnicodeError):raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT') from None
    validate_result(input_record,value)
