# Meeting AI

A self-hosted meeting transcription and analysis platform. Upload audio recordings, get accurate transcriptions with speaker identification, and receive intelligent summaries with action items.

## Features

- **Audio Transcription** — Supports MP3, M4A, WAV, and OGG files up to 300MB
- **Speaker Diarization** — Automatically identifies and labels different speakers
- **Language Detection** — Supports 99 languages with automatic detection
- **AI Analysis** — Generates summaries, key points, topics, and action items
- **Custom Instructions** — Guide the AI analysis with specific prompts
- **Speaker Identification** — Automatically detects speaker names from conversation context

## Tech Stack

| Layer | Technology |
|-------|------------|
| Framework | Next.js 16 (App Router) |
| Frontend | React 19, TypeScript 5 |
| Styling | Tailwind CSS 4, shadcn/ui |
| Database | PostgreSQL 16, Prisma ORM |
| Auth | NextAuth v5 |
| Transcription | AssemblyAI |
| AI Analysis | Google Gemini |

## Getting Started

### Prerequisites

- Node.js 24+ (CI/image use Node.js 26)
- PostgreSQL 16+ (or Docker)
- API keys for [AssemblyAI](https://www.assemblyai.com) and [Google AI Studio](https://aistudio.google.com)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/meeting-ai.git
cd meeting-ai
```

2. Install dependencies:
```bash
npm install
```

3. Set up environment variables:
```bash
cp .env.example .env.local
```

4. Generate required secrets:
```bash
# Encryption key for API keys
openssl rand -hex 32

# Auth secret
openssl rand -base64 32
```

5. Update `.env.local` with your values:
```env
DATABASE_URL="postgresql://postgres:password@localhost:5432/meeting_ai"
ENCRYPTION_KEY="<your-generated-hex-key>"
AUTH_SECRET="<your-generated-base64-key>"
```

6. Start PostgreSQL (using Docker):
```bash
docker run -d \
  --name postgres \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=meeting_ai \
  -p 5432:5432 \
  postgres:16
```

7. Initialize the database:
```bash
npx prisma migrate deploy
```

8. Start the development server:
```bash
npm run dev
```

9. Open [http://localhost:3000](http://localhost:3000) and log in with the default credentials from your `.env.local`.

10. Navigate to **Settings** to configure your AssemblyAI and Gemini API keys.

## Usage

1. **Upload** — Drag and drop an audio file or click to browse
2. **Add Instructions** (optional) — Provide context or specific analysis requirements
3. **Process** — The system transcribes and analyzes automatically
4. **Review** — View the transcript with speaker labels, summary, topics, and action items

## Project Structure

```
meeting-ai/
├── src/
│   ├── app/                 # Next.js App Router
│   │   ├── (auth)/          # Login pages
│   │   ├── (dashboard)/     # Protected routes
│   │   └── api/             # API endpoints
│   ├── components/          # React components
│   └── lib/
│       ├── config/          # App configuration
│       ├── db/              # Prisma client
│       ├── services/        # Business logic
│       └── utils/           # Helpers
├── prisma/                  # Database schema
└── docker/                  # Docker configuration
```

## Releases and deployment

Each app release has a stable SemVer (`0.1.0`, `0.1.1`, `0.2.0`), full Git revision and exact image digest. The first release uses `package.json` as its baseline; subsequent successful releases derive PATCH from routine commits, MINOR from `feat:`, and MAJOR from `!`/`BREAKING CHANGE:`. Failed builds and retries do not create new versions. The authenticated sidebar/API shows the actual image version, not a hardcoded package label.

`CI` runs tests, PostgreSQL migration qualification, build and typecheck. Manual Deploy defaults to `prepare_only=true`: build/qualify/publish the image and manifest without SSH or production rollout; ordinary successful main CI follows the enabled deployment policy. `Deploy` consumes successful CI for the current `main`, builds and qualifies one image, then asks the restricted host helper to prepare a consistent checkpoint, drain active writes/jobs, recreate only Meeting and verify identity/readiness. Only then is `vX.Y.Z` published with `deployed-release.json`. Use **Re-run failed jobs** after a tagging failure to retain the original qualified image; never overwrite a published version or silently rebuild it under the same number.

The web startup runs only `node server.js`. `0_init` is for a genuinely empty database. An existing database requires the read-only `scripts/check-migrations.mjs --mode baseline` checks and a separately reviewed `prisma migrate resolve --applied 0_init`; never execute the baseline SQL over live tables. Normal automatic deploy blocks pending migrations; future schema changes need a reviewed migration/compatibility batch before resuming.

The image requires `APP_VERSION`, `APP_REVISION`, `APP_BUILD_ID` at build time and a production bind at `/run/cloudbox/deploy`. Its parent is root/0755; `leases/` is UID1001/0700. A root-owned `draining` marker stops new mutations while already admitted uploads/jobs finish. Unreleased leases after a crash require inspection; do not erase them automatically. Public `/api/health` checks the database and returns only status; `/api/version` requires login and exposes only release identifiers.

Deployment configuration uses repository secrets `MEETING_DEPLOY_KEY` and `MEETING_SSH_KNOWN_HOSTS`, plus non-secret target variables `VPS_IP`, `SSH_PORT`, `DEPLOY_USER`. The key must be restricted to the root-owned Meeting helper; host identity is pinned, never discovered blindly during a deploy. The generic metadata helper in `scripts/release_manifest.py` is vendored from the infrastructure repository and must be kept consistent with its contract.

**D1 status:** implementation is prepared; production baseline/bootstrap and automatic Deploy activation remain pending. VPS-specific installation, rollback and backup procedures belong in the private infrastructure repository. Docker image qualification uses only disposable local containers and fixtures; it does not prove recovery of production data.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `ENCRYPTION_KEY` | 32-byte hex key for encrypting API keys |
| `AUTH_SECRET` | NextAuth.js secret |
| `AUTH_EMAIL` | Default admin email |
| `AUTH_PASSWORD` | Default admin password |

API keys (AssemblyAI and Gemini) are configured through the Settings page and stored encrypted in the database.

## License

MIT

Dependency qualification updates Next/Sharp and affected transitives. One known high advisory remains in `deepmerge-ts` through the Prisma CLI configuration ([GHSA-ggr8-5vv4-36mx](https://github.com/RebeccaStevens/deepmerge-ts/security/advisories/GHSA-ggr8-5vv4-36mx)); no HTTP input path was identified in the current static configuration. Track the upstream compatible fix. ESLint now runs with compatibility shims; six existing React Hooks errors remain outside the deployment changes. CI/build success does not imply a clean security audit or lint result.
