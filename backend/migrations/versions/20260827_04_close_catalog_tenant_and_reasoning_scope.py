"""close catalog tenant and reasoning scope

Revision ID: 20260827_04
Revises: 20260827_03
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_04"
down_revision: Union[str, None] = "20260827_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COLUMNS = {
    "data_asset_versions": ("tenant_id", "bucket_data_source_id"),
    "dataset_schemas": ("tenant_id",),
    "dataset_relations": ("tenant_id", "dataset_id"),
    "dataset_fields": ("tenant_id", "dataset_id", "schema_id"),
    "dataset_versions": ("tenant_id", "manifest_data_source_id"),
    "dataset_version_assets": ("tenant_id", "dataset_id"),
    "dataset_fragments": ("tenant_id", "dataset_id", "bucket_data_source_id"),
    "dataset_heads": ("tenant_id",),
    "ingestion_runs": ("trace_data_source_id",),
    "ingestion_run_inputs": ("tenant_id",),
    "scenario_dataset_bindings": ("tenant_id",),
    "semantic_mappings": ("tenant_id", "dataset_id"),
    "semantic_field_mappings": (
        "tenant_id",
        "scenario_id",
        "dataset_id",
        "dataset_schema_id",
        "dataset_relation_id",
        "ontology_entity_id",
    ),
    "semantic_relation_mappings": (
        "tenant_id",
        "dataset_id",
        "dataset_schema_id",
        "source_dataset_relation_id",
        "target_dataset_relation_id",
        "source_entity_id",
        "target_entity_id",
    ),
    "reasoning_terms": ("scenario_id", "scenario_scope_key"),
    "derivation_runs": ("trace_data_source_id",),
    "derivation_run_inputs": ("tenant_id",),
    "assertions": (
        "scenario_id",
        "predicate_entity_id",
        "subject_scenario_scope_key",
        "object_scenario_scope_key",
    ),
    "derivation_evidence": (
        "tenant_id",
        "derivation_run_id",
        "dataset_version_id",
        "dataset_relation_id",
        "document_data_source_id",
        "action_scenario_id",
    ),
}

REQUIRED_COLUMNS = {
    "data_asset_versions": ("tenant_id", "bucket_data_source_id"),
    "dataset_schemas": ("tenant_id",),
    "dataset_relations": ("tenant_id", "dataset_id"),
    "dataset_fields": ("tenant_id", "dataset_id", "schema_id"),
    "dataset_versions": ("tenant_id",),
    "dataset_version_assets": ("tenant_id", "dataset_id"),
    "dataset_fragments": ("tenant_id", "dataset_id", "bucket_data_source_id"),
    "dataset_heads": ("tenant_id",),
    "ingestion_run_inputs": ("tenant_id",),
    "scenario_dataset_bindings": ("tenant_id",),
    "semantic_mappings": ("tenant_id", "dataset_id"),
    "semantic_field_mappings": NEW_COLUMNS["semantic_field_mappings"],
    "semantic_relation_mappings": NEW_COLUMNS["semantic_relation_mappings"],
    "reasoning_terms": ("scenario_scope_key",),
    "derivation_run_inputs": ("tenant_id",),
    "assertions": ("subject_scenario_scope_key", "object_scenario_scope_key"),
    "derivation_evidence": ("tenant_id",),
}

UNIQUE_CONSTRAINTS = (
    ("uq_scenarios_id_tenant", "business_scenarios", ("id", "tenant_id")),
    ("uq_entities_id_scenario", "ontology_entities", ("id", "scenario_id")),
    ("uq_properties_id_entity", "ontology_properties", ("id", "entity_id")),
    (
        "uq_relations_id_scope",
        "ontology_relations",
        ("id", "scenario_id", "source_entity_id", "target_entity_id"),
    ),
    ("uq_relations_id_scenario", "ontology_relations", ("id", "scenario_id")),
    ("uq_instances_id_scenario", "ontology_instances", ("id", "scenario_id")),
    ("uq_data_sources_id_tenant", "data_sources", ("id", "tenant_id")),
    ("uq_bucket_files_id_source", "bucket_files", ("id", "data_source_id")),
    ("uq_document_chunks_id_source", "document_chunks", ("id", "data_source_id")),
    (
        "uq_snapshots_id_tenant_scenario",
        "ontology_snapshots",
        ("id", "tenant_id", "scenario_id"),
    ),
    (
        "uq_releases_id_tenant_scenario_snapshot",
        "ontology_releases",
        ("id", "tenant_id", "scenario_id", "snapshot_id"),
    ),
    ("uq_action_logs_id_scenario", "action_execution_logs", ("id", "scenario_id")),
    ("uq_data_assets_id_tenant", "data_assets", ("id", "tenant_id")),
    ("uq_asset_versions_id_tenant", "data_asset_versions", ("id", "tenant_id")),
    ("uq_logical_datasets_id_tenant", "logical_datasets", ("id", "tenant_id")),
    (
        "uq_dataset_schemas_id_scope",
        "dataset_schemas",
        ("id", "dataset_id", "tenant_id"),
    ),
    (
        "uq_dataset_relations_id_scope",
        "dataset_relations",
        ("id", "schema_id", "dataset_id", "tenant_id"),
    ),
    (
        "uq_dataset_fields_id_scope",
        "dataset_fields",
        ("id", "dataset_relation_id", "schema_id", "dataset_id", "tenant_id"),
    ),
    (
        "uq_dataset_fields_id_tenant_relation",
        "dataset_fields",
        ("id", "tenant_id", "dataset_relation_id"),
    ),
    ("uq_dataset_versions_id_tenant", "dataset_versions", ("id", "tenant_id")),
    (
        "uq_dataset_versions_id_dataset_tenant",
        "dataset_versions",
        ("id", "dataset_id", "tenant_id"),
    ),
    (
        "uq_dataset_versions_id_scope",
        "dataset_versions",
        ("id", "schema_id", "dataset_id", "tenant_id"),
    ),
    (
        "uq_dataset_fragments_id_tenant_relation",
        "dataset_fragments",
        ("id", "tenant_id", "dataset_relation_id"),
    ),
    (
        "uq_dataset_fragments_id_evidence_scope",
        "dataset_fragments",
        ("id", "tenant_id", "dataset_version_id", "dataset_relation_id"),
    ),
    (
        "uq_dataset_heads_id_scope",
        "dataset_heads",
        ("id", "dataset_id", "tenant_id"),
    ),
    ("uq_ingestion_runs_id_tenant", "ingestion_runs", ("id", "tenant_id")),
    (
        "uq_scenario_bindings_id_scope",
        "scenario_dataset_bindings",
        ("id", "scenario_id", "tenant_id", "dataset_id"),
    ),
    (
        "uq_semantic_mappings_id_scope",
        "semantic_mappings",
        (
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
        ),
    ),
    (
        "uq_semantic_mappings_id_binding_scope",
        "semantic_mappings",
        (
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
            "scenario_dataset_binding_id",
        ),
    ),
    ("uq_reasoning_terms_id_tenant", "reasoning_terms", ("id", "tenant_id")),
    (
        "uq_reasoning_terms_id_scope_key",
        "reasoning_terms",
        ("id", "tenant_id", "scenario_scope_key"),
    ),
    ("uq_derivation_runs_id_tenant", "derivation_runs", ("id", "tenant_id")),
    (
        "uq_derivation_runs_id_scope",
        "derivation_runs",
        ("id", "tenant_id", "scenario_id"),
    ),
    ("uq_assertions_id_tenant", "assertions", ("id", "tenant_id")),
    (
        "uq_assertions_id_tenant_run",
        "assertions",
        ("id", "tenant_id", "derivation_run_id"),
    ),
    (
        "uq_derivation_inputs_run_tenant_version",
        "derivation_run_inputs",
        ("derivation_run_id", "tenant_id", "dataset_version_id"),
    ),
)

CHECK_CONSTRAINTS = (
    (
        "ck_dataset_versions_manifest_pair",
        "dataset_versions",
        "(manifest_bucket_file_id IS NULL) = (manifest_data_source_id IS NULL)",
    ),
    (
        "ck_ingestion_runs_trace_pair",
        "ingestion_runs",
        "(trace_bucket_file_id IS NULL) = (trace_data_source_id IS NULL)",
    ),
    (
        "ck_reasoning_terms_kind_identity",
        "reasoning_terms",
        "(kind = 'ontology_instance' AND ontology_instance_id IS NOT NULL AND scenario_id IS NOT NULL) OR "
        "(kind = 'ontology_entity' AND ontology_entity_id IS NOT NULL AND scenario_id IS NOT NULL) OR "
        "(kind = 'dataset_record' AND dataset_version_id IS NOT NULL AND scenario_id IS NULL) OR "
        "(kind = 'iri' AND iri IS NOT NULL AND scenario_id IS NULL) OR "
        "(kind = 'literal' AND literal_value IS NOT NULL AND scenario_id IS NULL)",
    ),
    (
        "ck_reasoning_terms_scenario_scope_key",
        "reasoning_terms",
        "scenario_scope_key = coalesce(scenario_id, '')",
    ),
    (
        "ck_derivation_runs_snapshot_scenario",
        "derivation_runs",
        "ontology_snapshot_id IS NULL OR scenario_id IS NOT NULL",
    ),
    (
        "ck_derivation_runs_release_snapshot",
        "derivation_runs",
        "ontology_release_id IS NULL OR (scenario_id IS NOT NULL AND ontology_snapshot_id IS NOT NULL)",
    ),
    (
        "ck_derivation_runs_trace_pair",
        "derivation_runs",
        "(trace_bucket_file_id IS NULL) = (trace_data_source_id IS NULL)",
    ),
    (
        "ck_assertions_predicate_scope",
        "assertions",
        "(predicate_property_id IS NOT NULL AND predicate_entity_id IS NOT NULL AND scenario_id IS NOT NULL) OR "
        "(predicate_relation_id IS NOT NULL AND predicate_entity_id IS NULL AND scenario_id IS NOT NULL) OR "
        "(predicate_key IS NOT NULL AND predicate_entity_id IS NULL)",
    ),
    (
        "ck_assertions_term_scenario_scope",
        "assertions",
        "(subject_scenario_scope_key = '' OR "
        "(scenario_id IS NOT NULL AND subject_scenario_scope_key = scenario_id)) AND "
        "(object_scenario_scope_key = '' OR "
        "(scenario_id IS NOT NULL AND object_scenario_scope_key = scenario_id))",
    ),
    (
        "ck_derivation_evidence_one_source",
        "derivation_evidence",
        "(CASE WHEN evidence_assertion_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN dataset_fragment_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN document_chunk_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN action_execution_log_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN external_locator IS NULL THEN 0 ELSE 1 END) = 1",
    ),
    (
        "ck_derivation_evidence_dataset_scope",
        "derivation_evidence",
        "(dataset_fragment_id IS NULL AND dataset_field_id IS NULL "
        "AND dataset_relation_id IS NULL AND dataset_version_id IS NULL) OR "
        "(dataset_fragment_id IS NOT NULL AND dataset_relation_id IS NOT NULL "
        "AND dataset_version_id IS NOT NULL AND derivation_run_id IS NOT NULL)",
    ),
    (
        "ck_derivation_evidence_document_pair",
        "derivation_evidence",
        "(document_chunk_id IS NULL) = (document_data_source_id IS NULL)",
    ),
    (
        "ck_derivation_evidence_action_pair",
        "derivation_evidence",
        "(action_execution_log_id IS NULL) = (action_scenario_id IS NULL)",
    ),
)

FOREIGN_KEYS = (
    ("fk_asset_versions_asset_tenant", "data_asset_versions", "data_assets", ("asset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_asset_versions_file_source", "data_asset_versions", "bucket_files", ("bucket_file_id", "bucket_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_asset_versions_source_tenant", "data_asset_versions", "data_sources", ("bucket_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_schemas_dataset_tenant", "dataset_schemas", "logical_datasets", ("dataset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_relations_schema_scope", "dataset_relations", "dataset_schemas", ("schema_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_fields_relation_scope", "dataset_fields", "dataset_relations", ("dataset_relation_id", "schema_id", "dataset_id", "tenant_id"), ("id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_versions_dataset_tenant", "dataset_versions", "logical_datasets", ("dataset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_versions_schema_scope", "dataset_versions", "dataset_schemas", ("schema_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_versions_manifest_source", "dataset_versions", "bucket_files", ("manifest_bucket_file_id", "manifest_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_dataset_versions_source_tenant", "dataset_versions", "data_sources", ("manifest_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_version_assets_version_scope", "dataset_version_assets", "dataset_versions", ("dataset_version_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_version_assets_asset_tenant", "dataset_version_assets", "data_asset_versions", ("asset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_fragments_version_scope", "dataset_fragments", "dataset_versions", ("dataset_version_id", "schema_id", "dataset_id", "tenant_id"), ("id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_fragments_relation_scope", "dataset_fragments", "dataset_relations", ("dataset_relation_id", "schema_id", "dataset_id", "tenant_id"), ("id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_fragments_file_source", "dataset_fragments", "bucket_files", ("bucket_file_id", "bucket_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_dataset_fragments_source_tenant", "dataset_fragments", "data_sources", ("bucket_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_heads_dataset_tenant", "dataset_heads", "logical_datasets", ("dataset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_dataset_heads_version_scope", "dataset_heads", "dataset_versions", ("dataset_version_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_runs_dataset_tenant", "ingestion_runs", "logical_datasets", ("dataset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_runs_output_scope", "ingestion_runs", "dataset_versions", ("output_version_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_runs_trace_source", "ingestion_runs", "bucket_files", ("trace_bucket_file_id", "trace_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_ingestion_runs_source_tenant", "ingestion_runs", "data_sources", ("trace_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_inputs_run_tenant", "ingestion_run_inputs", "ingestion_runs", ("ingestion_run_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_inputs_asset_tenant", "ingestion_run_inputs", "data_asset_versions", ("asset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_ingestion_inputs_version_tenant", "ingestion_run_inputs", "dataset_versions", ("dataset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_lineage_upstream_tenant", "dataset_lineage_edges", "dataset_versions", ("upstream_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_lineage_downstream_tenant", "dataset_lineage_edges", "dataset_versions", ("downstream_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_lineage_run_tenant", "dataset_lineage_edges", "ingestion_runs", ("ingestion_run_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_scenario_bindings_scenario_tenant", "scenario_dataset_bindings", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "CASCADE"),
    ("fk_scenario_bindings_dataset_tenant", "scenario_dataset_bindings", "logical_datasets", ("dataset_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_scenario_bindings_head_scope", "scenario_dataset_bindings", "dataset_heads", ("dataset_head_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_scenario_bindings_version_scope", "scenario_dataset_bindings", "dataset_versions", ("dataset_version_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_serving_projections_version_tenant", "serving_projections", "dataset_versions", ("dataset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_mappings_scenario_tenant", "semantic_mappings", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "CASCADE"),
    ("fk_semantic_mappings_entity_scenario", "semantic_mappings", "ontology_entities", ("entity_id", "scenario_id"), ("id", "scenario_id"), "CASCADE"),
    ("fk_semantic_mappings_binding_scope", "semantic_mappings", "scenario_dataset_bindings", ("scenario_dataset_binding_id", "scenario_id", "tenant_id", "dataset_id"), ("id", "scenario_id", "tenant_id", "dataset_id"), "RESTRICT"),
    ("fk_semantic_mappings_schema_scope", "semantic_mappings", "dataset_schemas", ("dataset_schema_id", "dataset_id", "tenant_id"), ("id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_mappings_relation_scope", "semantic_mappings", "dataset_relations", ("dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"), ("id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_fields_mapping_scope", "semantic_field_mappings", "semantic_mappings", ("semantic_mapping_id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "dataset_relation_id", "ontology_entity_id"), ("id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "dataset_relation_id", "entity_id"), "CASCADE"),
    ("fk_semantic_fields_property_entity", "semantic_field_mappings", "ontology_properties", ("ontology_property_id", "ontology_entity_id"), ("id", "entity_id"), "CASCADE"),
    ("fk_semantic_fields_field_scope", "semantic_field_mappings", "dataset_fields", ("dataset_field_id", "dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"), ("id", "dataset_relation_id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_relations_scenario_tenant", "semantic_relation_mappings", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "CASCADE"),
    ("fk_semantic_relations_ontology_scope", "semantic_relation_mappings", "ontology_relations", ("ontology_relation_id", "scenario_id", "source_entity_id", "target_entity_id"), ("id", "scenario_id", "source_entity_id", "target_entity_id"), "CASCADE"),
    ("fk_semantic_relations_binding_scope", "semantic_relation_mappings", "scenario_dataset_bindings", ("scenario_dataset_binding_id", "scenario_id", "tenant_id", "dataset_id"), ("id", "scenario_id", "tenant_id", "dataset_id"), "RESTRICT"),
    ("fk_semantic_relations_relation_scope", "semantic_relation_mappings", "dataset_relations", ("dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"), ("id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_relations_source_mapping", "semantic_relation_mappings", "semantic_mappings", ("source_semantic_mapping_id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "source_dataset_relation_id", "source_entity_id", "scenario_dataset_binding_id"), ("id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "dataset_relation_id", "entity_id", "scenario_dataset_binding_id"), "RESTRICT"),
    ("fk_semantic_relations_target_mapping", "semantic_relation_mappings", "semantic_mappings", ("target_semantic_mapping_id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "target_dataset_relation_id", "target_entity_id", "scenario_dataset_binding_id"), ("id", "tenant_id", "scenario_id", "dataset_id", "dataset_schema_id", "dataset_relation_id", "entity_id", "scenario_dataset_binding_id"), "RESTRICT"),
    ("fk_semantic_relations_source_field", "semantic_relation_mappings", "dataset_fields", ("source_field_id", "source_dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"), ("id", "dataset_relation_id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_semantic_relations_target_field", "semantic_relation_mappings", "dataset_fields", ("target_field_id", "target_dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"), ("id", "dataset_relation_id", "schema_id", "dataset_id", "tenant_id"), "RESTRICT"),
    ("fk_reasoning_terms_scenario_tenant", "reasoning_terms", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_reasoning_terms_instance_scenario", "reasoning_terms", "ontology_instances", ("ontology_instance_id", "scenario_id"), ("id", "scenario_id"), "RESTRICT"),
    ("fk_reasoning_terms_entity_scenario", "reasoning_terms", "ontology_entities", ("ontology_entity_id", "scenario_id"), ("id", "scenario_id"), "RESTRICT"),
    ("fk_reasoning_terms_version_tenant", "reasoning_terms", "dataset_versions", ("dataset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_runs_scenario_tenant", "derivation_runs", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_runs_snapshot_scope", "derivation_runs", "ontology_snapshots", ("ontology_snapshot_id", "tenant_id", "scenario_id"), ("id", "tenant_id", "scenario_id"), "RESTRICT"),
    ("fk_derivation_runs_release_scope", "derivation_runs", "ontology_releases", ("ontology_release_id", "tenant_id", "scenario_id", "ontology_snapshot_id"), ("id", "tenant_id", "scenario_id", "snapshot_id"), "RESTRICT"),
    ("fk_derivation_runs_trace_source", "derivation_runs", "bucket_files", ("trace_bucket_file_id", "trace_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_derivation_runs_source_tenant", "derivation_runs", "data_sources", ("trace_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_inputs_run_tenant", "derivation_run_inputs", "derivation_runs", ("derivation_run_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_inputs_version_tenant", "derivation_run_inputs", "dataset_versions", ("dataset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_assertions_run_tenant", "assertions", "derivation_runs", ("derivation_run_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_assertions_run_scope", "assertions", "derivation_runs", ("derivation_run_id", "tenant_id", "scenario_id"), ("id", "tenant_id", "scenario_id"), "RESTRICT"),
    ("fk_assertions_scenario_tenant", "assertions", "business_scenarios", ("scenario_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_assertions_subject_tenant", "assertions", "reasoning_terms", ("subject_term_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_assertions_subject_scope", "assertions", "reasoning_terms", ("subject_term_id", "tenant_id", "subject_scenario_scope_key"), ("id", "tenant_id", "scenario_scope_key"), "RESTRICT"),
    ("fk_assertions_object_tenant", "assertions", "reasoning_terms", ("object_term_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_assertions_object_scope", "assertions", "reasoning_terms", ("object_term_id", "tenant_id", "object_scenario_scope_key"), ("id", "tenant_id", "scenario_scope_key"), "RESTRICT"),
    ("fk_assertions_property_entity", "assertions", "ontology_properties", ("predicate_property_id", "predicate_entity_id"), ("id", "entity_id"), "RESTRICT"),
    ("fk_assertions_entity_scenario", "assertions", "ontology_entities", ("predicate_entity_id", "scenario_id"), ("id", "scenario_id"), "RESTRICT"),
    ("fk_assertions_relation_scenario", "assertions", "ontology_relations", ("predicate_relation_id", "scenario_id"), ("id", "scenario_id"), "RESTRICT"),
    ("fk_assertions_supersedes_tenant", "assertions", "assertions", ("supersedes_assertion_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_assertion_tenant", "derivation_evidence", "assertions", ("assertion_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_assertion_run", "derivation_evidence", "assertions", ("assertion_id", "tenant_id", "derivation_run_id"), ("id", "tenant_id", "derivation_run_id"), "RESTRICT"),
    ("fk_derivation_evidence_run_tenant", "derivation_evidence", "derivation_runs", ("derivation_run_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_support_tenant", "derivation_evidence", "assertions", ("evidence_assertion_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_fragment_scope", "derivation_evidence", "dataset_fragments", ("dataset_fragment_id", "tenant_id", "dataset_relation_id"), ("id", "tenant_id", "dataset_relation_id"), "RESTRICT"),
    ("fk_derivation_evidence_fragment_input", "derivation_evidence", "dataset_fragments", ("dataset_fragment_id", "tenant_id", "dataset_version_id", "dataset_relation_id"), ("id", "tenant_id", "dataset_version_id", "dataset_relation_id"), "RESTRICT"),
    ("fk_derivation_evidence_pinned_input", "derivation_evidence", "derivation_run_inputs", ("derivation_run_id", "tenant_id", "dataset_version_id"), ("derivation_run_id", "tenant_id", "dataset_version_id"), "RESTRICT"),
    ("fk_derivation_evidence_version_tenant", "derivation_evidence", "dataset_versions", ("dataset_version_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_field_scope", "derivation_evidence", "dataset_fields", ("dataset_field_id", "tenant_id", "dataset_relation_id"), ("id", "tenant_id", "dataset_relation_id"), "RESTRICT"),
    ("fk_derivation_evidence_chunk_source", "derivation_evidence", "document_chunks", ("document_chunk_id", "document_data_source_id"), ("id", "data_source_id"), "RESTRICT"),
    ("fk_derivation_evidence_source_tenant", "derivation_evidence", "data_sources", ("document_data_source_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("fk_derivation_evidence_action_scenario", "derivation_evidence", "action_execution_logs", ("action_execution_log_id", "action_scenario_id"), ("id", "scenario_id"), "RESTRICT"),
    ("fk_derivation_evidence_action_tenant", "derivation_evidence", "business_scenarios", ("action_scenario_id", "tenant_id"), ("id", "tenant_id"), "RESTRICT"),
    ("data_asset_versions_tenant_id_fkey", "data_asset_versions", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_schemas_tenant_id_fkey", "dataset_schemas", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_relations_tenant_id_fkey", "dataset_relations", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_fields_tenant_id_fkey", "dataset_fields", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_versions_tenant_id_fkey", "dataset_versions", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_version_assets_tenant_id_fkey", "dataset_version_assets", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_fragments_tenant_id_fkey", "dataset_fragments", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("dataset_heads_tenant_id_fkey", "dataset_heads", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("ingestion_run_inputs_tenant_id_fkey", "ingestion_run_inputs", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("scenario_dataset_bindings_tenant_id_fkey", "scenario_dataset_bindings", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("semantic_mappings_tenant_id_fkey", "semantic_mappings", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("semantic_field_mappings_tenant_id_fkey", "semantic_field_mappings", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("semantic_relation_mappings_tenant_id_fkey", "semantic_relation_mappings", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("reasoning_terms_scenario_id_fkey", "reasoning_terms", "business_scenarios", ("scenario_id",), ("id",), "RESTRICT"),
    ("derivation_run_inputs_tenant_id_fkey", "derivation_run_inputs", "tenants", ("tenant_id",), ("id",), "CASCADE"),
    ("assertions_scenario_id_fkey", "assertions", "business_scenarios", ("scenario_id",), ("id",), "RESTRICT"),
    ("derivation_evidence_tenant_id_fkey", "derivation_evidence", "tenants", ("tenant_id",), ("id",), "CASCADE"),
)

INDEX_COLUMNS = tuple(
    (f"ix_{table_name}_{column_name}", table_name, column_name)
    for table_name, column_names in NEW_COLUMNS.items()
    for column_name in column_names
)

BACKFILLS = (
    """UPDATE data_asset_versions AS v
       SET tenant_id = a.tenant_id, bucket_data_source_id = f.data_source_id
      FROM data_assets AS a, bucket_files AS f
     WHERE a.id = v.asset_id AND f.id = v.bucket_file_id""",
    """UPDATE dataset_schemas AS s
       SET tenant_id = d.tenant_id
      FROM logical_datasets AS d
     WHERE d.id = s.dataset_id""",
    """UPDATE dataset_relations AS r
       SET tenant_id = s.tenant_id, dataset_id = s.dataset_id
      FROM dataset_schemas AS s
     WHERE s.id = r.schema_id""",
    """UPDATE dataset_fields AS f
       SET tenant_id = r.tenant_id, dataset_id = r.dataset_id, schema_id = r.schema_id
      FROM dataset_relations AS r
     WHERE r.id = f.dataset_relation_id""",
    """UPDATE dataset_versions AS v
       SET tenant_id = d.tenant_id
      FROM logical_datasets AS d
     WHERE d.id = v.dataset_id""",
    """UPDATE dataset_versions AS v
       SET manifest_data_source_id = f.data_source_id
      FROM bucket_files AS f
     WHERE f.id = v.manifest_bucket_file_id""",
    """UPDATE dataset_version_assets AS va
       SET tenant_id = v.tenant_id, dataset_id = v.dataset_id
      FROM dataset_versions AS v
     WHERE v.id = va.dataset_version_id""",
    """UPDATE dataset_fragments AS f
       SET tenant_id = v.tenant_id,
           dataset_id = v.dataset_id,
           bucket_data_source_id = bf.data_source_id
      FROM dataset_versions AS v, bucket_files AS bf
     WHERE v.id = f.dataset_version_id AND bf.id = f.bucket_file_id""",
    """UPDATE dataset_heads AS h
       SET tenant_id = d.tenant_id
      FROM logical_datasets AS d
     WHERE d.id = h.dataset_id""",
    """UPDATE ingestion_runs AS r
       SET trace_data_source_id = f.data_source_id
      FROM bucket_files AS f
     WHERE f.id = r.trace_bucket_file_id""",
    """UPDATE ingestion_run_inputs AS i
       SET tenant_id = r.tenant_id
      FROM ingestion_runs AS r
     WHERE r.id = i.ingestion_run_id""",
    """UPDATE scenario_dataset_bindings AS b
       SET tenant_id = d.tenant_id
      FROM logical_datasets AS d
     WHERE d.id = b.dataset_id""",
    """UPDATE semantic_mappings AS m
       SET tenant_id = b.tenant_id, dataset_id = b.dataset_id
      FROM scenario_dataset_bindings AS b
     WHERE b.id = m.scenario_dataset_binding_id""",
    """UPDATE semantic_field_mappings AS f
       SET tenant_id = m.tenant_id,
           scenario_id = m.scenario_id,
           dataset_id = m.dataset_id,
           dataset_schema_id = m.dataset_schema_id,
           dataset_relation_id = m.dataset_relation_id,
           ontology_entity_id = m.entity_id
      FROM semantic_mappings AS m
     WHERE m.id = f.semantic_mapping_id""",
    """UPDATE semantic_relation_mappings AS m
       SET tenant_id = b.tenant_id,
           dataset_id = b.dataset_id,
           dataset_schema_id = carrier.schema_id,
           source_dataset_relation_id = source_mapping.dataset_relation_id,
           target_dataset_relation_id = target_mapping.dataset_relation_id,
           source_entity_id = relation.source_entity_id,
           target_entity_id = relation.target_entity_id
      FROM scenario_dataset_bindings AS b,
           dataset_relations AS carrier,
           semantic_mappings AS source_mapping,
           semantic_mappings AS target_mapping,
           ontology_relations AS relation
     WHERE b.id = m.scenario_dataset_binding_id
       AND carrier.id = m.dataset_relation_id
       AND source_mapping.id = m.source_semantic_mapping_id
       AND target_mapping.id = m.target_semantic_mapping_id
       AND relation.id = m.ontology_relation_id""",
    """UPDATE reasoning_terms AS t
       SET scenario_id = COALESCE(
           (SELECT i.scenario_id FROM ontology_instances AS i
             WHERE i.id = t.ontology_instance_id),
           (SELECT e.scenario_id FROM ontology_entities AS e
             WHERE e.id = t.ontology_entity_id)
       )""",
    """UPDATE reasoning_terms
       SET scenario_scope_key = coalesce(scenario_id, '')""",
    """UPDATE derivation_runs AS r
       SET trace_data_source_id = f.data_source_id
      FROM bucket_files AS f
     WHERE f.id = r.trace_bucket_file_id""",
    """UPDATE derivation_run_inputs AS i
       SET tenant_id = r.tenant_id
      FROM derivation_runs AS r
     WHERE r.id = i.derivation_run_id""",
    """UPDATE assertions AS a
       SET predicate_entity_id = p.entity_id
      FROM ontology_properties AS p
     WHERE p.id = a.predicate_property_id""",
    """UPDATE assertions AS a
       SET scenario_id = COALESCE(
           (SELECT r.scenario_id FROM derivation_runs AS r
             WHERE r.id = a.derivation_run_id),
           (SELECT r.scenario_id FROM ontology_relations AS r
             WHERE r.id = a.predicate_relation_id),
           (SELECT e.scenario_id FROM ontology_entities AS e
             WHERE e.id = a.predicate_entity_id)
       )""",
    """UPDATE assertions AS a
       SET subject_scenario_scope_key = subject.scenario_scope_key,
           object_scenario_scope_key = object.scenario_scope_key
      FROM reasoning_terms AS subject, reasoning_terms AS object
     WHERE subject.id = a.subject_term_id AND object.id = a.object_term_id""",
    """UPDATE derivation_evidence AS e
       SET tenant_id = a.tenant_id, derivation_run_id = a.derivation_run_id
      FROM assertions AS a
     WHERE a.id = e.assertion_id""",
    """UPDATE derivation_evidence AS e
       SET dataset_relation_id = COALESCE(
           (SELECT f.dataset_relation_id FROM dataset_fragments AS f
             WHERE f.id = e.dataset_fragment_id),
           (SELECT f.dataset_relation_id FROM dataset_fields AS f
             WHERE f.id = e.dataset_field_id)
       )""",
    """UPDATE derivation_evidence AS e
       SET dataset_version_id = f.dataset_version_id
      FROM dataset_fragments AS f
     WHERE f.id = e.dataset_fragment_id""",
    """UPDATE derivation_evidence AS e
       SET document_data_source_id = c.data_source_id
      FROM document_chunks AS c
     WHERE c.id = e.document_chunk_id""",
    """UPDATE derivation_evidence AS e
       SET action_scenario_id = l.scenario_id
      FROM action_execution_logs AS l
     WHERE l.id = e.action_execution_log_id""",
)

PRECHECKS = (
    (
        "catalog tenant/source scope",
        """SELECT v.id
              FROM data_asset_versions AS v
              JOIN data_assets AS a ON a.id = v.asset_id
              JOIN bucket_files AS f ON f.id = v.bucket_file_id
              JOIN data_sources AS s ON s.id = f.data_source_id
             WHERE v.tenant_id IS DISTINCT FROM a.tenant_id
                OR v.bucket_data_source_id IS DISTINCT FROM f.data_source_id
                OR v.tenant_id IS DISTINCT FROM s.tenant_id
            UNION ALL
            SELECT f.id
              FROM dataset_fragments AS f
              JOIN dataset_versions AS v ON v.id = f.dataset_version_id
              JOIN dataset_relations AS r ON r.id = f.dataset_relation_id
              JOIN bucket_files AS b ON b.id = f.bucket_file_id
              JOIN data_sources AS s ON s.id = b.data_source_id
             WHERE f.tenant_id IS DISTINCT FROM v.tenant_id
                OR f.dataset_id IS DISTINCT FROM v.dataset_id
                OR r.schema_id IS DISTINCT FROM f.schema_id
                OR r.dataset_id IS DISTINCT FROM f.dataset_id
                OR r.tenant_id IS DISTINCT FROM f.tenant_id
                OR f.bucket_data_source_id IS DISTINCT FROM b.data_source_id
                OR f.tenant_id IS DISTINCT FROM s.tenant_id
            UNION ALL
            SELECT b.id
              FROM scenario_dataset_bindings AS b
              JOIN business_scenarios AS s ON s.id = b.scenario_id
              JOIN logical_datasets AS d ON d.id = b.dataset_id
             WHERE b.tenant_id IS DISTINCT FROM s.tenant_id
                OR b.tenant_id IS DISTINCT FROM d.tenant_id
            LIMIT 1""",
    ),
    (
        "catalog operational tenant scope",
        """SELECT r.id
              FROM ingestion_runs AS r
              JOIN logical_datasets AS d ON d.id = r.dataset_id
         LEFT JOIN dataset_versions AS v ON v.id = r.output_version_id
             WHERE r.tenant_id IS DISTINCT FROM d.tenant_id
                OR (v.id IS NOT NULL AND
                    (v.tenant_id IS DISTINCT FROM r.tenant_id OR
                     v.dataset_id IS DISTINCT FROM r.dataset_id))
            UNION ALL
            SELECT e.id
              FROM dataset_lineage_edges AS e
              JOIN dataset_versions AS u ON u.id = e.upstream_version_id
              JOIN dataset_versions AS d ON d.id = e.downstream_version_id
         LEFT JOIN ingestion_runs AS r ON r.id = e.ingestion_run_id
             WHERE e.tenant_id IS DISTINCT FROM u.tenant_id
                OR e.tenant_id IS DISTINCT FROM d.tenant_id
                OR (r.id IS NOT NULL AND e.tenant_id IS DISTINCT FROM r.tenant_id)
            UNION ALL
            SELECT p.id
              FROM serving_projections AS p
              JOIN dataset_versions AS v ON v.id = p.dataset_version_id
             WHERE p.tenant_id IS DISTINCT FROM v.tenant_id
            LIMIT 1""",
    ),
    (
        "semantic mapping scope",
        """SELECT m.id
              FROM semantic_mappings AS m
              JOIN business_scenarios AS s ON s.id = m.scenario_id
              JOIN ontology_entities AS e ON e.id = m.entity_id
              JOIN scenario_dataset_bindings AS b ON b.id = m.scenario_dataset_binding_id
              JOIN dataset_schemas AS ds ON ds.id = m.dataset_schema_id
              JOIN dataset_relations AS r ON r.id = m.dataset_relation_id
             WHERE m.tenant_id IS DISTINCT FROM s.tenant_id
                OR e.scenario_id IS DISTINCT FROM m.scenario_id
                OR b.scenario_id IS DISTINCT FROM m.scenario_id
                OR b.tenant_id IS DISTINCT FROM m.tenant_id
                OR b.dataset_id IS DISTINCT FROM m.dataset_id
                OR ds.dataset_id IS DISTINCT FROM m.dataset_id
                OR ds.tenant_id IS DISTINCT FROM m.tenant_id
                OR r.schema_id IS DISTINCT FROM m.dataset_schema_id
                OR r.dataset_id IS DISTINCT FROM m.dataset_id
                OR r.tenant_id IS DISTINCT FROM m.tenant_id
            UNION ALL
            SELECT f.id
              FROM semantic_field_mappings AS f
              JOIN semantic_mappings AS m ON m.id = f.semantic_mapping_id
              JOIN ontology_properties AS p ON p.id = f.ontology_property_id
              JOIN dataset_fields AS df ON df.id = f.dataset_field_id
             WHERE f.tenant_id IS DISTINCT FROM m.tenant_id
                OR f.scenario_id IS DISTINCT FROM m.scenario_id
                OR f.dataset_id IS DISTINCT FROM m.dataset_id
                OR f.dataset_schema_id IS DISTINCT FROM m.dataset_schema_id
                OR f.dataset_relation_id IS DISTINCT FROM m.dataset_relation_id
                OR f.ontology_entity_id IS DISTINCT FROM m.entity_id
                OR p.entity_id IS DISTINCT FROM f.ontology_entity_id
                OR df.tenant_id IS DISTINCT FROM f.tenant_id
                OR df.dataset_id IS DISTINCT FROM f.dataset_id
                OR df.schema_id IS DISTINCT FROM f.dataset_schema_id
                OR df.dataset_relation_id IS DISTINCT FROM f.dataset_relation_id
            LIMIT 1""",
    ),
    (
        "semantic relation endpoint scope",
        """SELECT m.id
              FROM semantic_relation_mappings AS m
              JOIN ontology_relations AS r ON r.id = m.ontology_relation_id
              JOIN scenario_dataset_bindings AS b ON b.id = m.scenario_dataset_binding_id
              JOIN semantic_mappings AS sm ON sm.id = m.source_semantic_mapping_id
              JOIN semantic_mappings AS tm ON tm.id = m.target_semantic_mapping_id
              JOIN dataset_fields AS sf ON sf.id = m.source_field_id
              JOIN dataset_fields AS tf ON tf.id = m.target_field_id
             WHERE r.scenario_id IS DISTINCT FROM m.scenario_id
                OR r.source_entity_id IS DISTINCT FROM m.source_entity_id
                OR r.target_entity_id IS DISTINCT FROM m.target_entity_id
                OR b.tenant_id IS DISTINCT FROM m.tenant_id
                OR b.dataset_id IS DISTINCT FROM m.dataset_id
                OR sm.entity_id IS DISTINCT FROM m.source_entity_id
                OR tm.entity_id IS DISTINCT FROM m.target_entity_id
                OR sm.scenario_dataset_binding_id IS DISTINCT FROM m.scenario_dataset_binding_id
                OR tm.scenario_dataset_binding_id IS DISTINCT FROM m.scenario_dataset_binding_id
                OR sm.dataset_relation_id IS DISTINCT FROM m.source_dataset_relation_id
                OR tm.dataset_relation_id IS DISTINCT FROM m.target_dataset_relation_id
                OR sf.dataset_relation_id IS DISTINCT FROM m.source_dataset_relation_id
                OR tf.dataset_relation_id IS DISTINCT FROM m.target_dataset_relation_id
                OR sm.dataset_schema_id IS DISTINCT FROM m.dataset_schema_id
                OR tm.dataset_schema_id IS DISTINCT FROM m.dataset_schema_id
            LIMIT 1""",
    ),
    (
        "reasoning tenant/snapshot scope",
        """SELECT t.id
              FROM reasoning_terms AS t
         LEFT JOIN business_scenarios AS s ON s.id = t.scenario_id
         LEFT JOIN dataset_versions AS v ON v.id = t.dataset_version_id
             WHERE (s.id IS NOT NULL AND s.tenant_id IS DISTINCT FROM t.tenant_id)
                OR (v.id IS NOT NULL AND v.tenant_id IS DISTINCT FROM t.tenant_id)
            UNION ALL
            SELECT r.id
              FROM derivation_runs AS r
         LEFT JOIN business_scenarios AS s ON s.id = r.scenario_id
         LEFT JOIN ontology_snapshots AS snap ON snap.id = r.ontology_snapshot_id
         LEFT JOIN ontology_releases AS rel ON rel.id = r.ontology_release_id
             WHERE (s.id IS NOT NULL AND s.tenant_id IS DISTINCT FROM r.tenant_id)
                OR (snap.id IS NOT NULL AND
                    (snap.tenant_id IS DISTINCT FROM r.tenant_id OR
                     snap.scenario_id IS DISTINCT FROM r.scenario_id))
                OR (rel.id IS NOT NULL AND
                    (rel.tenant_id IS DISTINCT FROM r.tenant_id OR
                     rel.scenario_id IS DISTINCT FROM r.scenario_id OR
                     rel.snapshot_id IS DISTINCT FROM r.ontology_snapshot_id))
            LIMIT 1""",
    ),
    (
        "assertion/evidence tenant scope",
        """SELECT a.id
              FROM assertions AS a
              JOIN reasoning_terms AS subject ON subject.id = a.subject_term_id
              JOIN reasoning_terms AS object ON object.id = a.object_term_id
         LEFT JOIN derivation_runs AS r ON r.id = a.derivation_run_id
         LEFT JOIN business_scenarios AS s ON s.id = a.scenario_id
             WHERE subject.tenant_id IS DISTINCT FROM a.tenant_id
                OR object.tenant_id IS DISTINCT FROM a.tenant_id
                OR subject.scenario_scope_key IS DISTINCT FROM a.subject_scenario_scope_key
                OR object.scenario_scope_key IS DISTINCT FROM a.object_scenario_scope_key
                OR (a.subject_scenario_scope_key <> '' AND
                    a.subject_scenario_scope_key IS DISTINCT FROM a.scenario_id)
                OR (a.object_scenario_scope_key <> '' AND
                    a.object_scenario_scope_key IS DISTINCT FROM a.scenario_id)
                OR (r.id IS NOT NULL AND r.tenant_id IS DISTINCT FROM a.tenant_id)
                OR (s.id IS NOT NULL AND s.tenant_id IS DISTINCT FROM a.tenant_id)
            UNION ALL
            SELECT e.id
              FROM derivation_evidence AS e
              JOIN assertions AS a ON a.id = e.assertion_id
         LEFT JOIN assertions AS support ON support.id = e.evidence_assertion_id
         LEFT JOIN dataset_fragments AS fragment ON fragment.id = e.dataset_fragment_id
         LEFT JOIN dataset_fields AS field ON field.id = e.dataset_field_id
         LEFT JOIN derivation_run_inputs AS pinned
                ON pinned.derivation_run_id = e.derivation_run_id
               AND pinned.tenant_id = e.tenant_id
               AND pinned.dataset_version_id = e.dataset_version_id
         LEFT JOIN data_sources AS source ON source.id = e.document_data_source_id
         LEFT JOIN business_scenarios AS scenario ON scenario.id = e.action_scenario_id
             WHERE e.tenant_id IS DISTINCT FROM a.tenant_id
                OR e.derivation_run_id IS DISTINCT FROM a.derivation_run_id
                OR (support.id IS NOT NULL AND support.tenant_id IS DISTINCT FROM e.tenant_id)
                OR (fragment.id IS NOT NULL AND
                    (fragment.tenant_id IS DISTINCT FROM e.tenant_id OR
                     fragment.dataset_version_id IS DISTINCT FROM e.dataset_version_id OR
                     fragment.dataset_relation_id IS DISTINCT FROM e.dataset_relation_id))
                OR (e.dataset_fragment_id IS NOT NULL AND pinned.id IS NULL)
                OR (e.dataset_field_id IS NOT NULL AND e.dataset_fragment_id IS NULL)
                OR (field.id IS NOT NULL AND
                    (field.tenant_id IS DISTINCT FROM e.tenant_id OR
                     field.dataset_relation_id IS DISTINCT FROM e.dataset_relation_id))
                OR (source.id IS NOT NULL AND source.tenant_id IS DISTINCT FROM e.tenant_id)
                OR (scenario.id IS NOT NULL AND scenario.tenant_id IS DISTINCT FROM e.tenant_id)
            LIMIT 1""",
    ),
)

IMMUTABLE_TABLES = (
    "data_asset_versions",
    "dataset_schemas",
    "dataset_relations",
    "dataset_fields",
    "dataset_versions",
    "dataset_version_assets",
    "dataset_fragments",
    "ingestion_run_inputs",
    "dataset_lineage_edges",
    "reasoning_terms",
    "derivation_run_inputs",
    "assertions",
    "derivation_evidence",
)

MIGRATION_LEDGER_TABLES = (
    "alembic_version",
    "platform_migration_runs",
    "platform_migration_checkpoints",
)


def _precheck(label: str, statement: str) -> None:
    safe_label = label.replace("'", "''")
    op.execute(
        sa.text(
            f"""DO $$
            DECLARE invalid_id text;
            BEGIN
              SELECT candidate.id::text INTO invalid_id
                FROM ({statement}) AS candidate
               LIMIT 1;
              IF invalid_id IS NOT NULL THEN
                RAISE EXCEPTION USING MESSAGE =
                  '{safe_label} precheck failed for id=' || invalid_id;
              END IF;
            END
            $$"""
        )
    )


def _restrict_default_runtime_role() -> None:
    immutable = ", ".join(IMMUTABLE_TABLES)
    ledger = ", ".join(MIGRATION_LEDGER_TABLES)
    op.execute(
        sa.text(
            f"""DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                REVOKE UPDATE, DELETE ON TABLE {immutable} FROM ontology_app;
                REVOKE INSERT, UPDATE, DELETE ON TABLE {ledger} FROM ontology_app;
              END IF;
            END
            $$"""
        )
    )


def upgrade() -> None:
    for table_name, column_names in NEW_COLUMNS.items():
        for column_name in column_names:
            op.add_column(
                table_name,
                sa.Column(column_name, sa.String(length=32), nullable=True),
            )

    for statement in BACKFILLS:
        op.execute(sa.text(statement))

    for table_name, column_names in REQUIRED_COLUMNS.items():
        null_predicate = " OR ".join(
            f"{column_name} IS NULL" for column_name in column_names
        )
        _precheck(
            f"{table_name} required scope",
            f"SELECT id FROM {table_name} WHERE {null_predicate} LIMIT 1",
        )
    for label, statement in PRECHECKS:
        _precheck(label, statement)

    for table_name, column_names in REQUIRED_COLUMNS.items():
        for column_name in column_names:
            op.alter_column(
                table_name,
                column_name,
                existing_type=sa.String(length=32),
                nullable=False,
            )

    for index_name, table_name, column_name in INDEX_COLUMNS:
        op.create_index(index_name, table_name, [column_name], unique=False)
    for constraint_name, table_name, columns in UNIQUE_CONSTRAINTS:
        op.create_unique_constraint(constraint_name, table_name, list(columns))
    op.drop_constraint(
        "ck_derivation_evidence_one_source",
        "derivation_evidence",
        type_="check",
    )
    for constraint_name, table_name, condition in CHECK_CONSTRAINTS:
        op.create_check_constraint(constraint_name, table_name, condition)
    for name, source, target, local_columns, remote_columns, ondelete in FOREIGN_KEYS:
        op.create_foreign_key(
            name,
            source,
            target,
            list(local_columns),
            list(remote_columns),
            ondelete=ondelete,
        )

    _restrict_default_runtime_role()


def downgrade() -> None:
    op.execute(
        sa.text(
            f"""DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                GRANT UPDATE, DELETE ON TABLE {', '.join(IMMUTABLE_TABLES)} TO ontology_app;
                GRANT INSERT, UPDATE, DELETE ON TABLE {', '.join(MIGRATION_LEDGER_TABLES)} TO ontology_app;
              END IF;
            END
            $$"""
        )
    )
    for name, source, _target, _local, _remote, _ondelete in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, source, type_="foreignkey")
    for constraint_name, table_name, _condition in reversed(CHECK_CONSTRAINTS):
        op.drop_constraint(constraint_name, table_name, type_="check")
    for constraint_name, table_name, _columns in reversed(UNIQUE_CONSTRAINTS):
        op.drop_constraint(constraint_name, table_name, type_="unique")
    for index_name, table_name, _column_name in reversed(INDEX_COLUMNS):
        op.drop_index(index_name, table_name=table_name)
    for table_name, column_names in reversed(tuple(NEW_COLUMNS.items())):
        for column_name in reversed(column_names):
            op.drop_column(table_name, column_name)
    op.create_check_constraint(
        "ck_derivation_evidence_one_source",
        "derivation_evidence",
        "(CASE WHEN evidence_assertion_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN dataset_fragment_id IS NULL AND dataset_field_id IS NULL "
        "THEN 0 ELSE 1 END + "
        "CASE WHEN document_chunk_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN action_execution_log_id IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN external_locator IS NULL THEN 0 ELSE 1 END) = 1",
    )
