# 文件职责：多阶段构建 H5 后交给 Nginx 提供静态资源；Node 只在构建阶段使用。
FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build:h5
# 运行阶段只保留 Nginx 与 H5 产物，避免把 Node 构建工具带入最终镜像。
FROM nginx:1.28-alpine
COPY --from=build /web/dist/build/h5 /usr/share/nginx/html
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
