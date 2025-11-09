# app/Dockerfile
FROM python:3.11-slim

# 1) 装 ffmpeg（Debian 官方包，内置 libopus/ogg 支持）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 2) Python 依赖
WORKDIR /app
COPY requirement.txt /app/requirement.txt
RUN pip install --no-cache-dir -r requirement.txt

# 3) 应用代码
COPY app.py /app/app.py
RUN mkdir -p /app/static/tts

ENV PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
