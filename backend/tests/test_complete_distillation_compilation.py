import hashlib
import json
from copy import deepcopy

import pytest

from app.services import scenario_model_compiler as compiler


def _handoff():
    content = "\n".join(f"Paragraph {index}: governed business definition." for index in range(1000))
    return {"id": "distillation:synthetic", "filename": "Governed handoff", "status": "parsed",
        "parsed_text": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "usage_plane": "modeling_material", "business_decision": "continue"}


def test_compilation_covers_the_complete_handoff_including_its_tail():
    document = _handoff()
    source = compiler.build_source_bundle("Compile", [], complete_handoffs=[document])
    assert source["documents"][0]["retrieval_complete"] is True
    assert source["documents"][0]["retrieved_characters"] == len(document["parsed_text"])
    handoff_paragraphs = [item for item in source["paragraphs"] if item["source_id"] == document["id"]]
    assert "Paragraph 999" in handoff_paragraphs[-1]["text"]
    assert source["documents"][0]["content_hash"] == document["content_hash"]


def test_handoff_rejects_changed_content_and_excess_size():
    from app.services.distillation_handoff_service import complete_compilation_documents
    document = _handoff()
    changed = deepcopy(document)
    changed["parsed_text"] += "tampered"
    with pytest.raises(ValueError, match="身份不一致"):
        complete_compilation_documents([changed], max_chars=100_000)
    with pytest.raises(ValueError, match="总量上限"):
        complete_compilation_documents([document], max_chars=10)


def test_structured_handoff_keeps_complete_entity_attributes_and_all_values():
    body = {'entities': [{'name': 'Record', 'attributes': [f'field_{i}' for i in range(180)]}],
            'decision': 'continue', 'open_questions': [], 'relations': []}
    content = json.dumps(body, indent=2)
    document = {**_handoff(), 'parsed_text': content,
                'content_hash': hashlib.sha256(content.encode()).hexdigest()}
    source = compiler.build_source_bundle('Compile', [], complete_handoffs=[document])
    units = [json.loads(item['text']) for item in source['paragraphs']
             if item['source_id'] == document['id']]
    reconstructed = {item['source_path'][0]: item['value'] for item in units}
    assert reconstructed == body
    entity_unit = next(item for item in units if item['source_path'] == ['entities'])
    assert len(entity_unit['value'][0]['attributes']) == 180


def test_large_handoff_list_splits_at_member_boundaries_without_losing_values():
    from app.services.distillation_handoff_service import _compilation_units
    body = {'entities': [{'name': f'Entity {i}', 'description': 'x' * 500} for i in range(30)]}
    units = [json.loads(item) for item in _compilation_units(json.dumps(body))]
    assert [item['source_path'] for item in units] == [['entities', i] for i in range(30)]
    assert [item['value'] for item in units] == body['entities']
