# 文件职责：构建后端运行镜像：锁定依赖、复制应用并使用非 root 用户启动 Uvicorn。
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 先复制锁文件安装依赖，源码修改可复用依赖层缓存。
COPY backend/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY backend /app
RUN useradd -m -u 10001 hai && mkdir -p /data/media /data/checkpoints && chown -R hai:hai /data /app
# 运行时不使用 root；媒体和检查点目录已预先授予应用用户。
USER hai
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
