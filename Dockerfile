# 档案管理模块 Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 系统依赖（Pillow 编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libjpeg-dev zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8080

CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]
