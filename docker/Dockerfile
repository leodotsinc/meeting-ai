# Base image
FROM node:26-alpine@sha256:c6ad9e9c1d80f6eb607c26b4b0a132ff7a2344fad1c78ea3baa2a7d4c8de6d94 AS base

# The current Node base predates Alpine's zlib security update.
RUN apk add --no-cache 'zlib>=1.3.2-r1'

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Copy package files
COPY package.json package-lock.json* ./
COPY vendor ./vendor

# Install dependencies
RUN npm ci --no-audit \
    && npm cache clean --force \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

# Build the application
FROM base AS builder
WORKDIR /app

# npm's bundled dependency tree is unnecessary after dependency installation.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

# Set dummy DATABASE_URL for build time (needed for Prisma and Next.js build)
ENV DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
ENV NEXT_TELEMETRY_DISABLED=1

# Copy node_modules from deps
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/vendor ./vendor
COPY . .

# Generate Prisma client
RUN node node_modules/prisma/build/index.js generate

# Build Next.js
RUN node node_modules/next/dist/bin/next build

# Production image
FROM base AS runner
WORKDIR /app

RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

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
COPY --from=builder --chown=nextjs:nodejs /app/vendor ./vendor

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
