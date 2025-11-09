import os, uuid, base64, hmac, hashlib
from datetime import datetime
from urllib.parse import urlencode
import json
import ssl
from flask import Flask, request, jsonify, send_from_directory
import requests
from websocket import create_connection

# ---- 环境变量（部署时写到 .env 或容器环境）----
APP_ID     = os.getenv("XF_APP_ID",     "your_app_id")
API_KEY    = os.getenv("XF_API_KEY",    "your_api_key")
API_SECRET = os.getenv("XF_API_SECRET", "your_api_secret")

# OpenAI 兼容大模型（可换成 DeepSeek/OpenAI/自建）
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY",  "your_llm_key")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # 兼容端点
OPENAI_MODEL    = os.getenv("OPENAI_MODEL",    "gpt-4o-mini") # 自行替换

# ---- Flask 基础 ----
app = Flask(__name__, static_folder="static", static_url_path="/static")
AUDIO_DIR = os.path.join(app.static_folder, "tts")
os.makedirs(AUDIO_DIR, exist_ok=True)

# ---------- 1) 调用大模型，返回纯文本 ----------
def call_llm(prompt: str) -> str:
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system","content": "Answer as a professional assistant, concise and clear."},
            {"role": "user","content": prompt}
        ],
        "temperature": 0.7,
        "stream": False
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# ---------- 2) 讯飞 TTS（WebSocket v2），保存为 mp3 ----------
def build_auth_url():
    # 讯飞 TTS WebSocket 入口（通用为 tts-api.xfyun.cn/v2/tts）
    host  = "tts-api.xfyun.cn"
    path  = "/v2/tts"
    scheme = "wss://"
    date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    # 拼签名串
    signature_origin = f"host: {host}\n"
    signature_origin += f"date: {date}\n"
    signature_origin += f"GET {path} HTTP/1.1"
    signature_sha = hmac.new(API_SECRET.encode('utf-8'),
                             signature_origin.encode('utf-8'),
                             digestmod=hashlib.sha256).digest()
    signature_base64 = base64.b64encode(signature_sha).decode('utf-8')

    authorization_origin = (
        f'api_key="{API_KEY}", algorithm="hmac-sha256", headers="host date request-line", '
        f'signature="{signature_base64}"'
    )
    authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')

    params = {
        "authorization": authorization,
        "date": date,
        "host": host
    }
    return scheme + host + path + "?" + urlencode(params)

def xunfei_tts(text: str, voice="x4_lingxia", aformat="audio/L16;rate=16000", encode="lame", bitrate="128k") -> str:
    """
    voice: 发音人代号（按你的账号可用项配置）
    aformat: 音频格式（PCM: audio/L16;rate=16000），也可用 "lame" 编码输出 mp3
    encode/bitrate: 当选择 lame 编码时控制输出 mp3
    返回：保存的相对URL（可直接给前端）
    """
    url = build_auth_url()
    ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})

    # 请求体（按讯飞TTS标准结构）
    req = {
        "common": {"app_id": APP_ID},
        "business": {
            "aue": "lame",           # mp3：lame；wav/pcm 可用 raw/其他；视套餐支持而定
            "auf": "audio/L16;rate=16000",
            "vcn": voice,           # 发音人
            "speed": 50,            # 0-100
            "volume": 50,           # 0-100
            "pitch": 50,            # 0-100
            "tte": "UTF8"
        },
        "data": {
            "status": 2,            # 2 表示一次性文本
            "text": base64.b64encode(text.encode("utf-8")).decode("utf-8")
        }
    }
    ws.send(json.dumps(req))

    audio_bytes = b""
    try:
        while True:
            resp = ws.recv()
            if not resp:
                break
            data = json.loads(resp)
            code = data["code"]
            if code != 0:
                raise RuntimeError(f"TTS error: {code}, {data.get('message')}")
            audio_chunk = data["data"]["audio"]
            audio_bytes += base64.b64decode(audio_chunk)
            if data["data"]["status"] == 2:
                break
    finally:
        ws.close()

    # 保存 mp3 文件
    fname = f"{uuid.uuid4().hex}.mp3"
    fpath = os.path.join(AUDIO_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(audio_bytes)

    # 返回可访问 URL
    return f"/static/tts/{fname}"

# ---------- 3) 对外 API ----------
@app.route("/api/ask", methods=["POST"])
def api_ask():
    payload = request.get_json(force=True)
    question = (payload or {}).get("question", "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    try:
        answer = call_llm(question)
        audio_url = xunfei_tts(answer)  # 也可以把 question 也合成一份，做问答双声道
        return jsonify({
            "question": question,
            "answer": answer,
            "audio_url": audio_url
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # 开发模式
    app.run(host="0.0.0.0", port=8000, debug=True)
