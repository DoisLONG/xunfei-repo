1、本机修改完，push 到 github 仓
2、15 机器上，/root/repo=>代码仓
3、docker build -t xfyun-voice:dev .
4、运行
docker run --rm -p 8001:8001 \
  -e XF_APP_ID="cb2aa48e" \
  -e XF_API_KEY="212c9afc1037b2fbfd0808549f295437" \
  -e XF_API_SECRET="ZDdhOTc4NzlkN2FjMzZjNjY0MjQxNWQw" \
  -e ENABLE_LLM=0 \
  -e DUMP_STREAM=1 \
  -v $(pwd)/static/tts:/app/static/tts \
  xfyun-voice:dev
--
1、前端：镜像构建，docker build -t xfyun-voice-frontend:latest .
2、运行：
docker run -d --name xfyun-voice-frontend -p 30280:80 xfyun-voice-frontend:latest

docker run -d --name xfyun-voice-frontend -p 30280:80 \
  --add-host=host.docker.internal:host-gateway \
  xfyun-voice-frontend:v0.4
