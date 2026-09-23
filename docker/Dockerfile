# Base image
FROM node:26-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070 AS base

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
