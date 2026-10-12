# Disposable CI tool scan, never an application image or deploy target.
FROM node:26-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070
WORKDIR /tools
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && npm sbom --sbom-format=cyclonedx > /tools/sbom.cdx.json \
    && rm -rf /root/.npm
