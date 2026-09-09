# Platform database migrations

PostgreSQL schema changes are applied by Alembic before an application process
starts. The API runtime role only verifies `alembic_version` and never executes
DDL. Run `alembic upgrade head` with the migration owner before starting the
application, then use the runtime verification script to confirm the deployed
revision and permissions.

Revision `20260909_29` adds object lifecycle policies, the rule input-validation
mode and manual-instance business identity uniqueness. Its backfill refuses
ambiguous keys or duplicate identities instead of merging records. Incomplete
legacy records retain a missing identity until explicitly corrected. Database
triggers serialize identity-definition changes and enforce manual initial states
and transitions; the runtime role performs no DDL. Immutable release JSON is not
rewritten. Downgrade refuses to discard enabled lifecycle policies.

Revision `20260909_30` canonicalizes numeric identity hashes so `1` and `1.0`
cannot create distinct manual objects. Original attribute values are retained.
Equivalent existing identities block migration and require explicit resolution.

## Business identity and manual publication

Revision `20260908_27` removes deployment labels from current business columns,
constraints, invocation keys, dataset heads, connectors, queues and audit rows.
It adds manual scenario release lifecycle state and append-only lifecycle audit.
Legacy immutable snapshots remain byte-identical and are decoded only by the
historical contract adapter. They never select current data or permissions.

Stop all API and worker instances before upgrading. Provide the existing payload
key ring: stored workflow and pending Agent inputs are authenticated, re-encrypted
without the retired dimension, and retain replay identity. Duplicate logical
bindings, heads, object identities or active releases cause a transactional
failure. Resolve conflicts explicitly from business ownership; migration never
chooses a deployment label, merges customer data, or publishes authored content.
Revision 27 cannot reconstruct removed partitions on downgrade; restore a
pre-upgrade backup. The verifier exercises reversible segments independently,
checks that this downgrade refuses, then restores the current head.

Revision `20260908_28` binds approval decisions to message identity and revision,
retains immutable evidence references and distinguishes retry generations.
Historical decisions without a known generation keep that absence explicit.
Only SELECT/INSERT is granted on evidence and release audit tables. Downgrade
refuses to discard recorded decisions, repeated execution history or evidence.

## Capability confirmation status

Revision `20260907_26` widens `capability_invocations.status` from 20 to 32
characters. The existing `awaiting_confirmation` token has 21 characters, so
PostgreSQL previously rejected successful gated previews before they could be
confirmed. State tokens, permissions and confirmation semantics are unchanged.
Downgrade locks the table and refuses to narrow it while a longer status remains;
reconcile those pending invocations first. No historical migration is rewritten.

## Workspace/account access cutover

Revision `20260907_25` separates global account roles from workspace roles, adds
session workspace selection, invitation delivery claims, revision checks,
append-only access audit and persistent auth throttling. The runtime receives
SELECT/INSERT on access audit and SELECT/UPDATE on the bootstrap guard, with no
table ownership or DDL privileges. Schema provisioning must retain the existing
runtime CRUD grants for the original control-plane tables.

Existing accounts remain ordinary system accounts. Choose a verified account
with `BOOTSTRAP_SUPERADMIN_EMAIL`; bootstrap runs once and records an audit.
Set `PUBLIC_APP_URL` to the HTTPS frontend origin and enable Secure cookies.
The migration revokes v1 browser sessions and verification codes; users log in
again or request a fresh verification code. No production identity is inferred.

Three durable-run actor references now point to the global User identity;
their parent/resource tenant foreign keys remain unchanged. Live membership
authorization is still required before enqueue and before worker execution.
Downgrade refuses to discard invitation/audit history, system role assignments,
cross-workspace memberships or cross-workspace execution attribution. Export
and reconcile those facts explicitly before attempting rollback.

## Workflow payload key prerequisite

Revision `20260908_27` preserves terminal Agent turn audit rows whose conversation
and both message references were already deleted. Their original encrypted
payload and fingerprint remain byte-for-byte unchanged because the deleted
authenticated context cannot be reconstructed. The runtime refuses to retry such
rows. All attached or nonterminal records must still authenticate and convert;
missing keys or invalid ciphertext on those records abort the entire migration.

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
`ontology_migration_verify_*` database, verifies the reversible migration segments,
irreversible guards and runtime-role boundaries, and drops the fixture in a `finally` cleanup:

```powershell
python scripts/verify_alembic_roundtrip.py
python scripts/verify_postgresql_runtime.py
```

Resolved on 2026-09-01 under explicit migration-governance approval: revision
`17` now restores the revision `16` definition and least-privilege grants for
`detach_data_source_file_references(...)` during downgrade. The isolated
verifier checks the head contract, the exact revision `16` function semantics
and permissions, absence at revision `09`, and the final return to head. No
live, shared, or customer database was downgraded for this verification.
The applied historical revisions have not been rewritten; repairing this path
requires an explicit migration-governance decision under the root constitution.

Apply forward migrations with the migration owner. Use the isolated verifier
above for downgrade rehearsals; the current head cannot be downgraded to 09:

```powershell
python -m alembic -x use_admin=1 upgrade head
```

Downgrade fails closed if different capabilities in one scenario now use the
same logical `port_key`, because the old scenario-level uniqueness constraint
cannot represent both contracts without data loss.

Run the downgrade rehearsal on an isolated database copy before any temporary
attachment expiry sweep. Never use a live customer database as a migration
round-trip fixture.
