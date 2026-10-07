# syntax=docker/dockerfile:1
#
# 一个镜像同时跑 API 和 worker —— 两者共用同一套代码和依赖，
# 分开构建只会让镜像多一份、还容易出现版本漂移。
# compose 里通过覆盖 command 来区分角色。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先拷依赖清单再安装：只改业务代码时，这一层能命中缓存，构建从分钟级降到秒级
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 用非 root 跑。容器里默认是 root，一旦被拿下就是宿主级别的风险
RUN useradd --create-home --uid 1000 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8100

# 默认起 API；worker 在 compose 里覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
