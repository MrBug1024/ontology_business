from app.services.distillation_tool_schema import portable_schema
from app.services.distillation_conversation_tools import definitions
from app.distillation_sample_schemas import DatabaseSampleArguments


def test_optional_file_references_are_strings_or_omitted():
    tool = next(item["function"] for item in definitions() if item["function"]["name"] == "read_database_sample")
    schema = tool["parameters"]
    assert schema["properties"]["bucket_file_id"]["type"] == "string"
    assert "anyOf" not in schema["properties"]["bucket_file_id"]
    assert "bucket_file_id" not in schema["required"]
    assert DatabaseSampleArguments(data_source_id="source").bucket_file_id is None


def test_required_unions_and_nonnullable_alternatives_are_preserved():
    schema = {"type": "object", "required": ["required_value"], "properties": {
        "required_value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "choice": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    assert portable_schema(schema) == schema


def test_nested_proposal_objects_are_explicit_for_compatible_model_endpoints():
    import json

    tool = next(item["function"] for item in definitions() if item["function"]["name"] == "propose_document")
    document = tool["parameters"]["properties"]["document"]
    assert document["type"] == "object"
    assert document["properties"]["as_is"]["type"] == "object"
    assert document["properties"]["entities"]["items"]["type"] == "object"
    assert '"$ref"' not in json.dumps(tool)
