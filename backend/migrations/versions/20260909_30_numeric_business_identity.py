"""Canonicalize equivalent numeric business keys without rewriting attributes.

Revision ID: 20260909_30
Revises: 20260909_29
"""
from alembic import op
import sqlalchemy as sa

revision = "20260909_30"
down_revision = "20260909_29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("LOCK TABLE ontology_instances, ontology_properties IN SHARE ROW EXCLUSIVE MODE")
    duplicates = op.get_bind().scalar(sa.text("""
        SELECT count(*) FROM (
            SELECT i.entity_id,
                CASE WHEN jsonb_typeof(i.attributes::jsonb -> p.name)='number' THEN
                    encode(sha256(convert_to(to_jsonb(trim_scale((i.attributes::jsonb ->> p.name)::numeric))::text,'UTF8')),'hex')
                ELSE i.business_key_hash END AS canonical_key
            FROM ontology_instances i JOIN ontology_properties p ON p.entity_id=i.entity_id AND p.is_key
            WHERE i.source='manual' AND i.business_key_hash IS NOT NULL
            GROUP BY i.entity_id,canonical_key HAVING count(*)>1
        ) conflicts
    """))
    if duplicates:
        raise RuntimeError("Equivalent numeric object identities exist; resolve them explicitly before migration.")
    op.execute("""
        CREATE FUNCTION public.canonicalize_numeric_object_identity() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        DECLARE key_name text; key_value jsonb;
        BEGIN
            IF NEW.source <> 'manual' THEN RETURN NEW; END IF;
            SELECT p.name INTO key_name FROM public.ontology_properties p
                WHERE p.entity_id=NEW.entity_id AND p.is_key;
            key_value := NEW.attributes::jsonb -> key_name;
            IF jsonb_typeof(key_value)='number' THEN
                NEW.business_key_hash := encode(sha256(convert_to(
                    to_jsonb(trim_scale((key_value #>> '{}')::numeric))::text,'UTF8')),'hex');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.canonicalize_numeric_object_identity() FROM PUBLIC")
    # PostgreSQL runs same-event triggers by name. Identity/type/state checks
    # in ontology_instance_contract lock the entity before this hash projection.
    op.execute("""CREATE TRIGGER ontology_instance_numeric_identity BEFORE INSERT OR UPDATE
        ON ontology_instances FOR EACH ROW EXECUTE FUNCTION public.canonicalize_numeric_object_identity()""")
    _recompute_numeric_keys()


def _recompute_numeric_keys() -> None:
    op.execute("""
        UPDATE ontology_instances i SET business_key_hash=NULL FROM ontology_properties p
        WHERE p.entity_id=i.entity_id AND p.is_key AND i.source='manual'
          AND jsonb_typeof(i.attributes::jsonb -> p.name)='number'
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE ontology_instances, ontology_properties IN SHARE ROW EXCLUSIVE MODE")
    op.execute("DROP TRIGGER ontology_instance_numeric_identity ON ontology_instances")
    op.execute("DROP FUNCTION public.canonicalize_numeric_object_identity()")
    _recompute_numeric_keys()
