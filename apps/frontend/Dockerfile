# ---------------------------------------------------------------------------
# マルチステージビルド。
#   builder : Vue (client/) を Vite でビルドして dist/ を作る
#   runtime : Express と OpenTelemetry の依存だけを入れ、dist/ を配信する
# vue / vite は devDependencies なので、実行イメージには入らない。
# ---------------------------------------------------------------------------
FROM node:22-slim AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.mts index.html ./
COPY client ./client
RUN npm run build

# ---------------------------------------------------------------------------
FROM node:22-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY server ./server
COPY --from=builder /app/dist ./dist

EXPOSE 3000

# Node の「ゼロコード計装」。--require で SDK を先に読み込ませる。
CMD ["npm", "start"]
