from __future__ import annotations

import pytest

from app.services import input_contract_validator


def _contract(*, cardinality: str = "one") -> dict:
    return {
        "port_key": "records",
        "required": True,
        "cardinality": cardinality,
        "binding_kinds": ["asset_version", "dataset_version"],
        "schema_document": {
            input_contract_validator.CONTENT_CONTRACT_KEY: {
                "version": "tabular-content/v1",
                "relations": [
                    {
                        "fields": [
                            {
                                "name": "record_id",
                                "aliases": ["Record ID"],
                                "logical_types": ["string"],
                                "required": True,
                            },
                            {
                                "name": "amount",
                                "logical_types": ["number"],
                                "required": True,
                            },
                        ],
                        "minimum_data_rows": 1,
                        "allow_additional_fields": True,
                    }
                ],
                "allow_additional_relations": True,
            }
        },
    }


def _table_profile(*, relation_name: str, amount_type: str = "integer") -> dict:
    return {
        "format": "catalog-file-profile/v1",
        "category": "table",
        "tables": [
            {
                "name": relation_name,
                "relation_name": relation_name,
                "sample_row_count": 2,
                "columns": [
                    {"name": "Record ID", "logical_type": "string"},
                    {"name": "amount", "logical_type": amount_type},
                    {"name": "unmodeled_note", "logical_type": "string"},
                ],
            }
        ],
    }


def _observed(index: int, profile: dict, *, kind: str = "asset_version"):
    return input_contract_validator.ObservedInput(
        index=index,
        binding_kind=kind,
        reference_id=f"version-{index}",
        profile=profile,
    )


def test_content_contract_ignores_file_and_relation_names() -> None:
    first = _table_profile(relation_name="original-file")
    renamed = _table_profile(relation_name="renamed-file")

    first_result = input_contract_validator.validate_profile(
        _contract()["schema_document"], first
    )
    renamed_result = input_contract_validator.validate_profile(
        _contract()["schema_document"], renamed
    )

    assert first_result.structural_fingerprint == renamed_result.structural_fingerprint
    assert first_result.matched is True
    assert renamed_result.matched is True


def test_structural_fingerprint_changes_when_record_count_changes() -> None:
    first = _table_profile(relation_name="records")
    changed = _table_profile(relation_name="records")
    changed["tables"][0]["sample_row_count"] = 3

    assert input_contract_validator.structural_fingerprint(
        first
    ) != input_contract_validator.structural_fingerprint(changed)


def test_contract_definition_validation_rejects_unknown_version() -> None:
    document = _contract()["schema_document"]
    document[input_contract_validator.CONTENT_CONTRACT_KEY]["version"] = "future/v9"

    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_content_contract(document)

    assert captured.value.code == "invalid_content_contract"


@pytest.mark.parametrize("invalid_contract", [None, [], "tabular-content/v1"])
def test_optional_contract_rejects_explicit_non_object_values(
    invalid_contract: object,
) -> None:
    assert input_contract_validator.validate_content_contract(
        {"type": "object", "properties": {}}
    ) is False

    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_content_contract(
            {input_contract_validator.CONTENT_CONTRACT_KEY: invalid_contract}
        )

    assert captured.value.code == "invalid_content_contract"


@pytest.mark.parametrize(
    ("target", "unknown_key"),
    [
        ("contract", "minimum_rows"),
        ("relation", "minimum_rows"),
        ("field", "column_name"),
    ],
)
def test_contract_definition_rejects_unknown_fields(
    target: str,
    unknown_key: str,
) -> None:
    document = _contract()["schema_document"]
    contract = document[input_contract_validator.CONTENT_CONTRACT_KEY]
    relation = contract["relations"][0]
    selected = {
        "contract": contract,
        "relation": relation,
        "field": relation["fields"][0],
    }[target]
    selected[unknown_key] = 1

    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_content_contract(document)

    assert captured.value.code == "invalid_content_contract"


def test_missing_empty_and_ambiguous_content_fail_closed() -> None:
    missing = _table_profile(relation_name="anything")
    missing["tables"][0]["columns"] = [{"name": "record_id", "logical_type": "string"}]
    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_profile(_contract()["schema_document"], missing)
    assert captured.value.code == "content_contract_missing"

    empty = _table_profile(relation_name="anything")
    empty["tables"][0]["sample_row_count"] = 0
    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_profile(_contract()["schema_document"], empty)
    assert captured.value.code == "content_contract_missing"

    ambiguous = _table_profile(relation_name="first")
    ambiguous["tables"].append(_table_profile(relation_name="second")["tables"][0])
    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_profile(_contract()["schema_document"], ambiguous)
    assert captured.value.code == "content_contract_ambiguous"


def test_many_cardinality_accepts_repeated_single_relation_bundle() -> None:
    bundle = _table_profile(relation_name="first")
    bundle["tables"].append(_table_profile(relation_name="second")["tables"][0])

    validation = input_contract_validator.validate_profile_for_cardinality(
        _contract(cardinality="many")["schema_document"],
        bundle,
        cardinality="many",
    )

    assert validation.matched is True
    assert validation.relation_matches == (0, 1)


def test_many_cardinality_does_not_guess_repeated_multi_relation_groups() -> None:
    port = _contract(cardinality="many")
    contract = port["schema_document"][input_contract_validator.CONTENT_CONTRACT_KEY]
    contract["relations"].append(
        {
            "fields": [
                {
                    "name": "secondary_id",
                    "logical_types": ["string"],
                    "required": True,
                }
            ],
            "minimum_data_rows": 1,
            "allow_additional_fields": True,
        }
    )
    primary = _table_profile(relation_name="primary")
    secondary = {
        "name": "secondary",
        "sample_row_count": 1,
        "columns": [{"name": "secondary_id", "logical_type": "string"}],
    }
    bundle = {
        "category": "table",
        "tables": [
            primary["tables"][0],
            secondary,
            _table_profile(relation_name="primary-again")["tables"][0],
            {**secondary, "name": "secondary-again"},
        ],
    }

    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.validate_profile_for_cardinality(
            port["schema_document"],
            bundle,
            cardinality="many",
        )

    assert captured.value.code == "content_contract_ambiguous"


def test_no_contract_only_consumes_basic_required_input_and_keeps_extras() -> None:
    ports = [
        {
            "port_key": "document",
            "required": True,
            "cardinality": "one",
            "binding_kinds": ["asset_version"],
            "schema_document": {},
        }
    ]
    observed = [
        _observed(0, {"category": "document"}),
        _observed(1, {"category": "document"}),
    ]

    result = input_contract_validator.match_inputs(ports, observed)

    assert result.assignments[0].port_key == "document"
    assert result.assignments[0].input_indices == (0,)
    assert result.supplementary_indices == (1,)


def test_content_matches_required_port_and_preserves_unmatched_supplementary_file() -> None:
    result = input_contract_validator.match_inputs(
        [_contract()],
        [
            _observed(0, {"category": "document"}),
            _observed(1, _table_profile(relation_name="renamed")),
        ],
    )

    assert result.assignments[0].port_key == "records"
    assert result.assignments[0].input_indices == (1,)
    assert result.supplementary_indices == (0,)


def test_many_port_accepts_every_compatible_content_input() -> None:
    result = input_contract_validator.match_inputs(
        [_contract(cardinality="many")],
        [
            _observed(0, _table_profile(relation_name="alpha")),
            _observed(1, _table_profile(relation_name="beta"), kind="dataset_version"),
        ],
    )

    assert result.assignments[0].port_key == "records"
    assert result.assignments[0].input_indices == (0, 1)
    assert result.supplementary_indices == ()


def test_one_input_matching_two_contract_ports_is_ambiguous() -> None:
    alternate = {**_contract(), "port_key": "alternate"}

    with pytest.raises(input_contract_validator.InputContractError) as captured:
        input_contract_validator.match_inputs(
            [_contract(), alternate],
            [_observed(0, _table_profile(relation_name="renamed"))],
        )

    assert captured.value.code == "content_contract_ambiguous"
