# Meeting AI: agent guidance

- App source and delivery live here. VPS-wide inventory, policies and recovery are maintained in the private `leodots/vps-bootstrap` repository; do not copy host secrets, production data or private evidence here.
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
- All mutating API handlers and detached processing must retain a deployment lease. Add coverage when adding new mutations; never bypass draining to make a rollout succeed. Production requires the protected control bind.
- Run `npm test`, Python tests, build/typecheck and appropriate isolated qualification after changes. ESLint10 compatibility is fixed; six pre-existing React Hooks errors remain. Do not report lint passed or expand delivery work into unrelated UI refactors.
- D1 bootstrap was verified on 2026-09-14 with v0.1.0: baseline metadata, control bind and restricted deployment access are installed; existing data, uploads and authenticated session were preserved. Do not reapply the initial bootstrap. Follow the current verified release and workflow state for routine updates. Independent disaster recovery and new provider processing remain separate, unverified acceptance steps.
