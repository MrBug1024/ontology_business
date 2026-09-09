"""Persist object identity and explicit business semantics.

Revision ID: 20260909_29
Revises: 20260908_28
"""
from alembic import op
import sqlalchemy as sa

revision = "20260909_29"
down_revision = "20260908_28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ontology_entities", sa.Column("state_policy", sa.JSON(), nullable=False,
                                               server_default=sa.text("'{}'::json")))
    op.add_column("ontology_rules", sa.Column("input_validation", sa.String(20), nullable=False,
                                            server_default="object"))
    op.create_check_constraint("ck_rule_input_validation", "ontology_rules",
                               "input_validation IN ('object', 'record')")
    op.add_column("ontology_instances", sa.Column("business_key_hash", sa.String(64)))
    op.execute("LOCK TABLE ontology_instances, ontology_properties IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("""
        SELECT count(*) FROM (SELECT entity_id FROM ontology_properties
        WHERE is_key GROUP BY entity_id HAVING count(*)>1) ambiguous
    """)):
        raise RuntimeError("Ambiguous object identity definitions exist; resolve them explicitly before migration.")
    # Incomplete legacy facts retain NULL identity and remain visible as
    # incomplete. Never guess a key or merge existing business records.
    op.execute("""
        UPDATE ontology_instances i
        SET business_key_hash = encode(sha256(convert_to((i.attributes::jsonb -> p.name)::text, 'UTF8')), 'hex')
        FROM ontology_properties p
        WHERE p.entity_id=i.entity_id AND p.is_key AND i.source='manual'
          AND jsonb_typeof(i.attributes::jsonb -> p.name) IN ('string','number','boolean')
          AND i.attributes::jsonb ->> p.name <> ''
    """)
    duplicate_count = op.get_bind().scalar(sa.text("""
        SELECT count(*) FROM (
            SELECT entity_id, business_key_hash FROM ontology_instances
            WHERE business_key_hash IS NOT NULL GROUP BY entity_id,business_key_hash HAVING count(*)>1
        ) conflicts
    """))
    if duplicate_count:
        raise RuntimeError("Duplicate manual business identities exist; resolve them explicitly before migration.")
    op.create_unique_constraint("uq_instances_business_key", "ontology_instances",
                                ["entity_id", "business_key_hash"])
    op.execute("""
        CREATE FUNCTION public.enforce_ontology_instance_contract() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        DECLARE key_name text; key_count integer; policy jsonb; old_state text;
        BEGIN
            IF TG_OP='UPDATE' AND (NEW.entity_id IS DISTINCT FROM OLD.entity_id
                OR NEW.source IS DISTINCT FROM OLD.source) THEN
                RAISE EXCEPTION 'An existing object cannot change its identity scope'
                    USING ERRCODE='23514', CONSTRAINT='ck_instance_identity';
            END IF;
            NEW.business_key_hash := NULL;
            IF NEW.source <> 'manual' THEN RETURN NEW; END IF;
            SELECT e.state_policy::jsonb INTO policy FROM public.ontology_entities e
                WHERE e.id=NEW.entity_id FOR UPDATE;
            SELECT count(*), min(p.name) INTO key_count,key_name FROM public.ontology_properties p
                WHERE p.entity_id=NEW.entity_id AND p.is_key;
            IF key_count>1 THEN
                RAISE EXCEPTION 'Object identity definition is ambiguous'
                    USING ERRCODE='23514', CONSTRAINT='ck_instance_identity';
            END IF;
            IF key_count=1 THEN
                IF coalesce(jsonb_typeof(NEW.attributes::jsonb -> key_name),'null') NOT IN ('string','number','boolean')
                   OR NEW.attributes::jsonb ->> key_name = '' THEN
                    RAISE EXCEPTION 'Object business identity is required'
                        USING ERRCODE='23514', CONSTRAINT='ck_instance_identity';
                END IF;
                NEW.business_key_hash := encode(sha256(convert_to((NEW.attributes::jsonb -> key_name)::text,'UTF8')),'hex');
            END IF;
            IF coalesce((policy->>'enabled')::boolean,false) THEN
                IF TG_OP='INSERT' THEN
                    IF NOT (coalesce(policy->'initial_states','[]'::jsonb) ? coalesce(NEW.state,'')) THEN
                        RAISE EXCEPTION 'Initial object state is not allowed'
                            USING ERRCODE='23514', CONSTRAINT='ck_instance_state_policy';
                    END IF;
                ELSE
                    old_state := coalesce(OLD.state,'');
                    IF coalesce(NEW.state,'') <> old_state AND NOT EXISTS (
                        SELECT 1 FROM jsonb_array_elements(coalesce(policy->'transitions','[]'::jsonb)) t
                        WHERE t->>'from_state'=old_state AND t->>'to_state'=NEW.state
                    ) THEN
                        RAISE EXCEPTION 'Object state transition is not allowed'
                            USING ERRCODE='23514', CONSTRAINT='ck_instance_state_policy';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.enforce_ontology_instance_contract() FROM PUBLIC")
    op.execute("""CREATE TRIGGER ontology_instance_contract BEFORE INSERT OR UPDATE
        ON ontology_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_ontology_instance_contract()""")
    op.execute("""
        CREATE FUNCTION public.protect_ontology_identity_definition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        DECLARE object_id text; changes_key boolean;
        BEGIN
            IF TG_OP='INSERT' THEN object_id:=NEW.entity_id; changes_key:=NEW.is_key;
            ELSIF TG_OP='DELETE' THEN object_id:=OLD.entity_id; changes_key:=OLD.is_key;
            ELSE
                object_id:=OLD.entity_id;
                changes_key:=(OLD.is_key OR NEW.is_key) AND
                    (ROW(OLD.name,OLD.data_type,OLD.is_key,OLD.entity_id) IS DISTINCT FROM
                     ROW(NEW.name,NEW.data_type,NEW.is_key,NEW.entity_id));
            END IF;
            IF changes_key THEN
                PERFORM 1 FROM public.ontology_entities WHERE id=object_id FOR UPDATE;
                IF FOUND AND EXISTS (SELECT 1 FROM public.ontology_instances
                    WHERE entity_id=object_id AND source='manual') THEN
                    RAISE EXCEPTION 'Existing object identities require an explicit migration'
                        USING ERRCODE='23514', CONSTRAINT='ck_instance_identity';
                END IF;
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.protect_ontology_identity_definition() FROM PUBLIC")
    op.execute("""CREATE TRIGGER ontology_identity_definition BEFORE INSERT OR UPDATE OR DELETE
        ON ontology_properties FOR EACH ROW EXECUTE FUNCTION public.protect_ontology_identity_definition()""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM ontology_entities WHERE state_policy::jsonb->>'enabled'='true'")):
        raise RuntimeError("Cannot discard enabled object lifecycle policies; disable them explicitly first.")
    op.execute("DROP TRIGGER ontology_instance_contract ON ontology_instances")
    op.execute("DROP FUNCTION public.enforce_ontology_instance_contract()")
    op.execute("DROP TRIGGER ontology_identity_definition ON ontology_properties")
    op.execute("DROP FUNCTION public.protect_ontology_identity_definition()")
    op.drop_constraint("uq_instances_business_key", "ontology_instances", type_="unique")
    op.drop_column("ontology_instances", "business_key_hash")
    op.drop_constraint("ck_rule_input_validation", "ontology_rules", type_="check")
    op.drop_column("ontology_rules", "input_validation")
    op.drop_column("ontology_entities", "state_policy")
