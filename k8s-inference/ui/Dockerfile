FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY templates/default.conf.template /etc/nginx/templates/default.conf.template
# API_UPSTREAM/DNS_RESOLVER: the same-origin proxy; PUBLIC_API_URL/GRAFANA_URLS: served at /config.json (set per fleet)
ENV API_UPSTREAM=http://api.api.svc.cluster.local:80 DNS_RESOLVER=10.2.0.10 PUBLIC_API_URL= GRAFANA_URLS=
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
