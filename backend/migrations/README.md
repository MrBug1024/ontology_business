# Platform database migrations

PostgreSQL schema changes are applied by Alembic before an application process
starts. The API runtime role only verifies `alembic_version` and never executes
DDL. Run `alembic upgrade head` with the migration owner before starting the
application, then use the runtime verification script to confirm the deployed
revision and permissions.

## Workflow payload key prerequisite

Revision `20260829_09` replaces plaintext `workflow_runs.input_params` with an
AES-256-GCM envelope. Before upgrading a database that already contains workflow
runs, provide both `WORKFLOW_PAYLOAD_ACTIVE_KEY_ID` and
`WORKFLOW_PAYLOAD_ENCRYPTION_KEYS` to the Alembic process. The latter is a JSON
object mapping stable key ids to URL-safe base64 encoded 32-byte keys.

Keep historical key ids in the deployment secret manager while any queued,
retryable, approval-waiting or retained run references them. The migration
aborts transactionally when rows exist and the key ring is missing or invalid;
it never discards plaintext or substitutes a non-recoverable hash. Downgrading
also requires every referenced key and intentionally restores the legacy
plaintext column, so it must be treated as an explicit security rollback.

Never commit the key ring to an environment file, migration file, database row
or application log.

## Capability port ownership prerequisite

Revision `20260829_10` assigns every capability port to exactly one Function,
Action, or Workflow. Existing ports are backfilled only from explicit
`config.contract_source` evidence and an already-governed draft resolution.
The migration intentionally fails when ownership is missing or ambiguous; do
not repair it by guessing from names, prefixes, or the number of capabilities
in a scenario.

Revision `20260829_11` keeps the content hash and logical identity of expired
invocation attachments while detaching their physical `BucketFile` pair. The
runtime role still has no table-level `UPDATE` on `data_asset_versions`; it can
only execute the migration-owned, fail-closed expiry transition. Once any blob
has been detached and scheduled for deletion, downgrade to `10` is rejected
because a migration cannot reconstruct the removed object.

Revision `20260829_12` records who withdrew a staging/prod Release, when, and
why. Downgrading to `11` removes those structured withdrawal fields and is only
appropriate inside an explicitly accepted audit rollback window.

Validate the reversible path against a real PostgreSQL database before release.
The preferred command creates a uniquely named isolated
`ontology_migration_verify_*` database, verifies head -> `09` -> head plus the
runtime-role boundaries, and drops the fixture in a `finally` cleanup:

```powershell
python scripts/verify_alembic_roundtrip.py
python scripts/verify_postgresql_runtime.py
```

The equivalent manual commands below are for an already verified isolated
database only. Never point them at a live or shared database:

```powershell
python -m alembic -x use_admin=1 upgrade 20260829_12
python -m alembic -x use_admin=1 downgrade 20260829_09
python -m alembic -x use_admin=1 upgrade 20260829_12
```

Downgrade fails closed if different capabilities in one scenario now use the
same logical `port_key`, because the old scenario-level uniqueness constraint
cannot represent both contracts without data loss.

Run the downgrade rehearsal on an isolated database copy before any temporary
attachment expiry sweep. Never use a live customer database as a migration
round-trip fixture.
