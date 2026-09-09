"""One property-contract interpretation for rule providers and workflow nodes."""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .ontology_value_schema import property_schema
from .policies import PolicyViolation


def input_schema(rule: Any, entity: Any, fields: list[str]) -> dict:
    properties = {key: prop for prop in (getattr(entity, "properties", []) or [])
                  for key in {str(prop.name), str(prop.api_name or prop.name)}}
    constrained = getattr(rule, "input_validation", "record") == "object"
    if constrained and entity is not None and any(field not in properties for field in fields):
        raise PolicyViolation("规则引用了对象类型未定义的属性")
    return {"type": "object", "properties": {
        field: property_schema(properties.get(field), constrained=constrained) for field in fields
    }, "required": fields, "additionalProperties": False}


def validate_record(rule: Any, record: dict, entity: Any, fields: list[str]) -> None:
    from .ontology_service import _validate_property_value

    schema = input_schema(rule, entity, fields)
    # Direct evaluation historically accepts extra contextual fields. Only
    # validate the fields the rule consumes; provider envelopes remain closed.
    values = {field: record[field] for field in fields if field in record}
    error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(values), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path)
        raise PolicyViolation(f"规则输入{('属性“' + path + '”') if path else ''}不符合所选对象契约（{error.validator}）")
    for prop in getattr(entity, "properties", []) or []:
        for field in {prop.name, prop.api_name or prop.name} & set(values):
            try:
                _validate_property_value(prop, values[field], strict_type=True)
            except ValueError as exc:
                raise PolicyViolation(str(exc)) from exc
