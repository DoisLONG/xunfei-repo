# ====== Stage 1: 下载 ffmpeg 静态版 ======
FROM alpine:3.20 AS ffmpeg-get
RUN apk add --no-cache curl xz && \
    curl -L -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    mkdir -p /tmp/ffmpeg && \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1

# ====== Stage 2: 应用镜像 ======
FROM python:3.11-slim
WORKDIR /app

# 拷贝静态 ffmpeg
COPY --from=ffmpeg-get /tmp/ffmpeg/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-get /tmp/ffmpeg/ffprobe /usr/local/bin/ffprobe
RUN chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe

# Python 依赖与代码
COPY requirement.txt /app/requirement.txt
RUN pip install --no-cache-dir -r requirement.txt
COPY app.py /app/app.py
RUN mkdir -p /app/static/tts

ENV PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8001"]
