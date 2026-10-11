# 22-alpine as of 2026-10: pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

# 1.27-alpine as of 2026-10: pinned by digest so that a rebuild gets the same base; the tag is the one resolved by `crane digest`
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0
# Alpine security updates of the base image (trivy in CI fails on a fixed CRITICAL); back to the nginx user
USER root
RUN apk upgrade --no-cache
USER 101
COPY templates/default.conf.template /etc/nginx/templates/default.conf.template
# API_UPSTREAM/DNS_RESOLVER: the same-origin proxy; PUBLIC_API_URL/GRAFANA_URLS: served at /config.json (set per fleet)
ENV API_UPSTREAM=http://api.api.svc.cluster.local:80 DNS_RESOLVER=kube-dns.kube-system.svc.cluster.local PUBLIC_API_URL= GRAFANA_URLS=
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
