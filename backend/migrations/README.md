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

## Scenario-bound external access

Revision `20260918_35` binds each external API key to one explicitly selected
scenario through a tenant-composite foreign key and a validated constraint that
requires every active key to have a scenario. Historical keys have no reliable
scenario ownership evidence, so migration revokes them and records deterministic
`scenario_binding_required` lifecycle audit events. An authorized user must issue
a replacement for the intended scenario; the migration never guesses ownership.
Downgrade also revokes any active scoped keys with `scenario_binding_rollback`
audit events before removing the boundary. It never reactivates historical keys.

External upload ownership is stored in `external_scenario_assets` with composite
tenant foreign keys to its asset and scenario. The runtime role receives only
SELECT, INSERT and DELETE; UPDATE is forbidden so ownership cannot be reassigned.
Deleting an asset removes its ownership row, while the scenario foreign key uses
RESTRICT. Scenario retirement retains this evidence, and the physical purge plan
blocks deletion while scoped assets remain. Explicitly confirmed purge includes
scenario keys in its audit-deletion counts and removes their associated lifecycle
audit through the existing cascade.

The read-only runtime verifier checks these exact grants, the active-key binding
constraint and all three validated tenant-composite foreign keys. Test migration
and rollback only in an isolated PostgreSQL database.

Revision `20260918_36` introduces tenant-scoped business distillation projects
and immutable modeling-material publications. Each publication atomically stores
the reviewed document plus seven UTF-8 files (Markdown, Mermaid and JSON) in
PostgreSQL and creates a `distillation` catalog source. These are bounded modeling
documents, not MinIO objects or runtime datasets. Publication identity is unique
per project revision; retries return the same publication. Runtime grants allow
project reads/inserts/updates and publication reads/inserts only. Database triggers
also protect published content and catalog sources from modification/deletion.
The provenance file freezes connector revisions and selected file content/index
identities; it records that a live database's rows are not copied into the evidence.

Published evidence remains retained when a scenario is retired; the purge plan
explicitly blocks physical scenario deletion while a distillation project exists.
Downgrade is allowed only on an empty distillation schema and refuses to discard
existing drafts or publications. Verify the empty-schema round trip and populated
schema refusal in an isolated database. The read-only runtime verifier checks the
new table permissions along with the installed schema head.

Revision `20260918_37` adds tenant-composite conversation ownership, unique
per-project request IDs and turn numbers, and a partial unique index allowing
only one queued/running investigation per project. Durable turns record the
initiator, frozen project revision/evidence identity, bounded history/checkpoints,
tool summaries, clarification questions and unadopted proposals. A database
claim uses SKIP LOCKED; lease tokens, generations and expiry fence every write.
Independent heartbeat transactions renew long provider/read-only tool calls.
Expired claims resume from the last completed read-only round with at most three
attempts and ten provider calls. Cancellation clears ownership so late results
cannot overwrite the cancelled state. Asking a human atomically saves the
question and ends the turn; only an explicit, revision-checked adoption updates
the project. Existing history prevents moving the project to another scenario.

The runtime role receives SELECT, INSERT and UPDATE only. Website observations
store a sanitized bounded excerpt, response hash, retrieval time and exact
coverage; accepted evidence references the actual saved tool receipt. Connector
configuration and website access grants are excluded from modeling contracts.
The read-only runtime verifier checks table grants, ownership/idempotency
constraints and the valid unique active-turn index. Empty-schema downgrade to
the preceding revision is supported; any retained conversation history makes
downgrade fail closed. Rehearse only in the isolated acceptance database.

Revision `20260918_38` adds temporary conversation inputs and append-only turn
links. Each link uses composite foreign keys fixing its tenant, project and
initiating user on both the turn and input. Uploads are idempotent per project,
user and request ID. They cannot be transferred to another scenario, including
before the first message is sent. First use is explicit; later turns inherit
only the same user's previously submitted, still-live inputs in that conversation.
Unsent uploads are never inherited. No DataSource or BucketFile is created.

Original bytes are staged temporarily, parsed in a restricted child process,
and removed immediately. Only a bounded parsed excerpt and SHA-256 identity are
kept for 24 hours. Limits are 10 MiB raw bytes, 200,000 parsed characters, 32 MiB
expanded Office content, and 35 seconds wall time; the parser also has CPU and
memory limits. Multi-worker cleanup uses bounded SKIP LOCKED batches, erases
expired/removed text and raw tool checkpoints, and fences active turns. Conversation
messages, human-adopted findings and audit metadata remain retained; expiry is
not a claim that all derived information has been deleted.

Runtime grants allow SELECT/INSERT/UPDATE on input rows and SELECT/INSERT only
on turn links. The read-only deployment verifier checks these exact grants,
validated composite ownership constraints, retention checks and cleanup indexes.
The empty-schema downgrade is reversible. Any retained attachment history makes
downgrade refuse rather than discard ownership or evidence; test only in the
isolated acceptance database.
