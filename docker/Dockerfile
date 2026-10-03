# Base image
FROM node:26-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070 AS base
# npm 11.19.1 bundles vulnerable libraries. Patch only these bundled copies,
# preserving the pinned Node base and checking registry tarballs by SHA-512.
RUN set -eu; \
    for spec in brace-expansion@5.0.12 ip-address@10.7.1 undici@6.28.1; do \
      archive="$(npm pack "$spec" --silent --pack-destination /tmp)"; \
      case "$spec" in \
        brace-expansion@5.0.12) checksum=628bd0debce168b308ac38cd0cc90d4b4d6d79af77aa11211b9c72f1febe47497e770dca8bee6c0a822823de368ae2d94c75a881692d830543854d3d88d12299;; \
        ip-address@10.7.1) checksum=e0e500a94f59d62def0a74b4e61cc68859c4303a50fbada9003fcc5503a9f377d8c8d0bc1a5782a9a4b44229100667160ab285894b3f07cced4518983805ee08;; \
        undici@6.28.1) checksum=cd6a5d4d50f9e07e3c088c9b2f4ad6437ba4a5bf5ddb7c0cede1f946d7dd99e3fbd1c587363b5fa3b3f8bd95fee42a0370ee772783eee6e5e9ee535f8dce8344;; \
      esac; \
      printf '%s  %s\n' "$checksum" "/tmp/$archive" | sha512sum -c -; \
      name="${spec%@*}"; dest="/usr/local/lib/node_modules/npm/node_modules/$name"; \
      rm -rf "$dest"; mkdir -p "$dest"; \
      tar -xzf "/tmp/$archive" -C "$dest" --strip-components=1; \
      rm "/tmp/$archive"; \
    done; \
    npm --version

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Copy package files
COPY package.json package-lock.json* ./

# Install dependencies
RUN npm ci

# Build the application
FROM base AS builder
WORKDIR /app

# Set dummy DATABASE_URL for build time (needed for Prisma and Next.js build)
ENV DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
ENV NEXT_TELEMETRY_DISABLED=1

# Copy node_modules from deps
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Generate Prisma client
RUN npx prisma generate

# Build Next.js
RUN npm run build

# Production image
FROM base AS runner
WORKDIR /app

ARG APP_VERSION
ARG APP_REVISION
ARG APP_BUILD_ID
ENV APP_VERSION=$APP_VERSION
ENV APP_REVISION=$APP_REVISION
ENV APP_BUILD_ID=$APP_BUILD_ID
ENV DEPLOY_CONTROL_DIR=/run/cloudbox/deploy
LABEL io.cloudbox.deployment.drain="file-v1"
LABEL org.opencontainers.image.version=$APP_VERSION
LABEL org.opencontainers.image.revision=$APP_REVISION
LABEL org.opencontainers.image.source="https://github.com/leodotsinc/meeting-ai"
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy public folder
COPY --from=builder /app/public ./public

# Set the correct permission for prerender cache
RUN mkdir .next
RUN chown nextjs:nodejs .next

# Copy standalone build
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

# Copy prisma files and ALL node_modules (needed for migrations)
COPY --from=builder --chown=nextjs:nodejs /app/prisma.config.ts ./prisma.config.ts
COPY --from=builder --chown=nextjs:nodejs /app/prisma ./prisma
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules

# Migration checks run in a separate one-shot container with the migrator credential.
COPY --from=builder --chown=nextjs:nodejs /app/scripts ./scripts

# Copy entrypoint script
COPY --chown=nextjs:nodejs docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

# Create uploads directory
RUN mkdir -p /app/meeting-ai/uploads && chown nextjs:nodejs /app/meeting-ai/uploads

USER nextjs

EXPOSE 3003

ENV PORT=3003
ENV HOSTNAME="0.0.0.0"

# Start the web app with its limited runtime credential; never mutate schema here.
CMD ["./entrypoint.sh"]
