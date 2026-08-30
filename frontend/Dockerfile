# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS build
WORKDIR /frontend
COPY package.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm install -g npm@12.0.2 --no-audit --no-fund \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm install --no-audit --no-fund
COPY . .
RUN npm run typecheck && npm run test && npm run build

FROM nginx:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=5s --retries=12 --start-period=20s \
  CMD wget -q -O - http://127.0.0.1/healthz || exit 1
