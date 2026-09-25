# Meeting AI: agent guidance

## GitHub Actions budget

Iterate and validate locally; do not open a PR or push a commit for each trial.
A draft PR waits for `ready_for_review`. A push to `main` is a candidate for CI
and automatic deployment, so publish only the reviewed final batch. Concurrency
may cancel superseded PR checks, never `main` CI or deployment. Diagnose failed
runs before retrying; do not skip CI to publish.

- App source and delivery live here. VPS-wide inventory, policies and recovery are maintained in the private `leodots/cloudbox-infra` repository; do not copy host secrets, production data or private evidence here.
- Production uses Docker Compose, a limited PostgreSQL runtime role and a separate migrator. Never run `prisma db push`, reset/restore a live database, start schema changes with runtime credentials, or use generic `compose down`/global prune in deployment.
- Preserve authentication/encryption keys and uploads. A new release must retain existing sessions/data; HTTP readiness is distinct from authenticated acceptance, audio-provider processing and disaster recovery.
- Release versions use stable SemVer. After the initial package version, routine commits increment PATCH, `feat:` increments MINOR, and `!`/`BREAKING CHANGE:` increments MAJOR. Classify commits honestly. Create the immutable `vX.Y.Z` release only after the exact image has been verified on the server. A retry is not a new version.
- CI tests source, migration guards and an isolated database. Deploy builds and
  qualifies one image, then uses the common Cloudbox transport with an app-specific
  forced key and a short-lived GHCR token sent by stdin. The Meeting helper still
  owns drain, backup, migration refusal, health and rollback. No image selected by
  `latest`. New pending database migrations stop automatic deployment and require
  a reviewed migration batch.
- The shared transport is pinned by SHA and retains sanitized gateway failures in
  run/attempt-specific artifacts. Publication consumes `rollout.outputs.result_artifact`.
  A timeout/disconnection leaves runtime state unknown; inspect the host checkpoint
  before retrying the original release manifest. Never rebuild a consumed version.
- After publication, the pinned shared reconciliation workflow submits that
  verified receipt through the destination-only `cloudbox-release-reconciler`
  GitHub App and waits for the canonical `cloudbox-infra` PR/check/merge workflow.
  Repair reconciliation failures without changing the deployed release identity
  or bypassing the destination allowlist.
- All mutating API handlers and detached processing must retain a deployment lease. Add coverage when adding new mutations; never bypass draining to make a rollout succeed. Production requires the protected control bind.
- Run `npm test`, Python tests, build/typecheck and appropriate isolated qualification after changes. ESLint10 compatibility is fixed; six pre-existing React Hooks errors remain. Do not report lint passed or expand delivery work into unrelated UI refactors.
- D1 bootstrap was verified on 2026-09-14 with v0.1.0: baseline metadata, control bind and restricted deployment access are installed; existing data, uploads and authenticated session were preserved. Do not reapply the initial bootstrap. Follow the current verified release and workflow state for routine updates. Independent disaster recovery and new provider processing remain separate, unverified acceptance steps.

## Qualified maintenance adoption (cloudbox-infra PR 11)

- Mend Renovate installation was verified on 2026-09-21. The shared `leodotsinc/.github` preset owns npm/lockfile, Dockerfile and workflow discovery when this branch is merged; overlapping Dependabot version jobs are removed in the same change. Dependabot vulnerability alerts remain enabled. No AI reviewer or model call is added.
- PR and release image builds scan the immutable runtime and builder image IDs with pinned Trivy, a fresh database and bounded seven-day sanitized evidence. Unknown/stale results and high/critical findings refuse publication; passing source tests alone is insufficient.
- The ordinary release gate inspects all commits since the last verified published release, dependency/runtime/workflow paths and GitHub associated-PR metadata. Unqualified maintenance cannot reach production through a squash merge or a later feature commit. Read-only image preparation remains available; there is no calendar or label bypass.
- This repository's maintenance is not yet qualified for autonomous deployment. Cloudbox must bind the reviewed candidate to its policy, approved window, backup and runtime acceptance before enabling the dedicated executor. These changes do not prove installation or a production maintenance pilot.

## Isolated maintenance data qualification

- `scripts/qualify-image.py` uses an internal Docker network with no published ports; HTTP probes run on loopback inside only its random-named containers. It receives no production credentials or provider destinations.
- The fixture seeds one synthetic project/upload, captures a logical dump and upload bytes, restores into a different database and container, then verifies authenticated project reads, the existing session and exact upload hash. The original database/app stays intact. Hosted CI retains sanitized proof for seven days.
- This qualifies disposable backup recovery for the tested image. It does not qualify a cross-version upgrade/downgrade, production role/data coverage, real provider processing, a monthly receiver, or an automatic rollout. The Node digest candidate and cumulative unreleased dependency changes retain separate review gates.
