import os, uuid, hmac, hashlib, base64, json, ssl, subprocess, threading, queue, time, asyncio
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests
from websocket import create_connection

# ========= 环境配置 =========
# 单套密钥（一个应用同时开 IAT+TTS）
XF_APP_ID     = os.getenv("XF_APP_ID", "")
XF_API_KEY    = os.getenv("XF_API_KEY", "")
XF_API_SECRET = os.getenv("XF_API_SECRET", "")

# 双套密钥（可选，若设置则优先生效）
IAT_APP_ID     = os.getenv("XF_IAT_APP_ID", XF_APP_ID)
IAT_API_KEY    = os.getenv("XF_IAT_API_KEY", XF_API_KEY)
IAT_API_SECRET = os.getenv("XF_IAT_API_SECRET", XF_API_SECRET)

TTS_APP_ID     = os.getenv("XF_TTS_APP_ID", XF_APP_ID)
TTS_API_KEY    = os.getenv("XF_TTS_API_KEY", XF_API_KEY)
TTS_API_SECRET = os.getenv("XF_TTS_API_SECRET", XF_API_SECRET)

# 大模型（可接 Ollama/OpenAI 兼容；不开也能跑）
OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL= os.getenv("OPENAI_MODEL", "llama3.1")
ENABLE_LLM  = os.getenv("ENABLE_LLM", "1").lower() in ("1","true","yes")

# 调试：是否把上行分片落盘到 /tmp（1/true/yes 开启）
DUMP_STREAM = os.getenv("DUMP_STREAM", "0").lower() in ("1","true","yes")

# ========= FastAPI =========
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
os.makedirs("static/tts", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ========= 公共工具 =========
def xf_auth_url(host: str, path: str, api_key: str, api_secret: str) -> str:
    date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    signature_src = f"host: {host}\n" + f"date: {date}\n" + f"GET {path} HTTP/1.1"
    sha = hmac.new(api_secret.encode(), signature_src.encode(), hashlib.sha256).digest()
    signature_b64 = base64.b64encode(sha).decode()
    auth_origin = f'api_key="{api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_b64}"'
    authorization = base64.b64encode(auth_origin.encode()).decode()
    return f"wss://{host}{path}?" + urlencode({"authorization": authorization, "date": date, "host": host})

def call_llm(prompt: str) -> str:
    url = f"{OPENAI_BASE}/chat/completions"
    headers = {}
    if OPENAI_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_KEY}"
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role":"system","content":"You are a concise Chinese assistant."},
            {"role":"user","content":prompt}
        ],
        "temperature": 0.3, "stream": False
    }
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def mock_answer(user_text: str) -> str:
    t = (user_text or "").strip()
    if not t: return "我在呢，你可以先问我一个问题。"
    low = t.lower()
    if "时间" in t or "几点" in t or "time" in low:
        return f"现在时间是 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。"
    if "你是谁" in t or "who are you" in low:
        return "我是你的语音助手，正在帮你验证语音识别与合成链路。"
    return f"收到，你说：{t}"

def xunfei_tts(text: str, vcn="x4_lingxia") -> str:
    url = xf_auth_url("tts-api.xfyun.cn", "/v2/tts", TTS_API_KEY, TTS_API_SECRET)
    ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
    req = {
        "common": {"app_id": TTS_APP_ID},
        "business": {"aue":"lame","auf":"audio/L16;rate=16000","vcn":vcn,"speed":50,"volume":50,"pitch":50,"tte":"UTF8"},
        "data": {"status":2,"text": base64.b64encode(text.encode("utf-8")).decode()}
    }
    ws.send(json.dumps(req))
    audio = b""
    try:
        while True:
            resp = ws.recv()
            if not resp: break
            j = json.loads(resp)
            if j["code"] != 0:
                print("[TTS] error:", j.get("code"), j.get("message"), "sid=", j.get("sid"))
                raise RuntimeError(f"TTS error {j['code']} {j.get('message')}")
            audio += base64.b64decode(j["data"]["audio"])
            if j["data"]["status"] == 2: break
    finally:
        ws.close()
    name = f"{uuid.uuid4().hex}.mp3"
    path = os.path.join("static/tts", name)
    with open(path, "wb") as f: f.write(audio)
    return f"/static/tts/{name}"

def guess_ffmpeg_demux(mime: str):
    m = (mime or "").lower()
    if "webm" in m: return "webm"
    if "ogg"  in m: return "ogg"
    if "mp4"  in m or "m4a" in m: return "mp4"
    if "wav"  in m: return "wav"
    return None

# ========= IAT 会话 =========
class IATSession:
    def __init__(self, on_partial, on_final, mime: str = ""):
        self.on_partial = on_partial
        self.on_final = on_final
        self.mime = mime
        self.closed = False
        self.ffmpeg = None
        self.ws = None
        self._pcm_q = queue.Queue()
        self._ws_send_started = False
        self._in_bytes = 0
        self._dump = open(f"/tmp/iat_in.{(self.mime.split(';')[0] or 'bin').split('/')[-1]}", "wb") if DUMP_STREAM else None

        # 1) 建 IAT 连接
        url = xf_auth_url("iat-api.xfyun.cn", "/v2/iat", IAT_API_KEY, IAT_API_SECRET)
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})

        # 2) 同步：立即发送首帧（status=0，带 common/business），避免 10165
        try:
            silence = b"\x00" * 1280  # 40ms 静音
            first_payload = {
                "common": {"app_id": IAT_APP_ID},
                "business": {
                    "language":"zh_cn","domain":"iat","accent":"mandarin",
                    "ptt":1,"vinfo":1,"vad_eos":800
                },
                "data": {
                    "status":0,
                    "format":"audio/L16;rate=16000","encoding":"raw",
                    "audio": base64.b64encode(silence).decode()
                }
            }
            self.ws.send(json.dumps(first_payload))
            self._ws_send_started = True
            print("[IAT] first frame sent")
        except Exception as e:
            print("[IAT] initial first frame failed:", e)

        # 3) 起 ffmpeg 解复用（按 MIME）
        demux = guess_ffmpeg_demux(self.mime)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        if demux:
            cmd += ["-f", demux]

        # 关键：对从管道读入的容器格式（webm/ogg/mp4）启用流式参数，降低探测时延
        cmd += [
            "-fflags", "+genpts+nobuffer",   # 生成时间戳 + 低缓冲
            "-flags", "low_delay",
            "-probesize", "32k",
            "-analyzeduration", "0",
            "-use_wallclock_as_timestamps", "1",
        ]
        if demux in ("webm", "mp4", "ogg"):
            cmd += ["-seekable", "0"]        # 关键：从管道读 WebM/MP4/Ogg 时需标记为不可寻址

        cmd += [
            "-i", "pipe:0",
            "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"
        ]
        print("[FFMPEG]", " ".join(cmd))

        self.ffmpeg = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

        # 把 ffmpeg 的 stderr 打印出来，便于定位解码问题
        def _drain_stderr():
            while True:
                line = self.ffmpeg.stderr.readline()
                if not line:
                    break
                try:
                    print("[FFMPEG-ERR]", line.decode(errors="ignore").strip())
                except Exception:
                    pass
        threading.Thread(target=_drain_stderr, daemon=True).start()

        # 4) 线程：读 PCM / 送 IAT / 收结果
        threading.Thread(target=self._read_ffmpeg_pcm, daemon=True).start()
        threading.Thread(target=self._pump_pcm_to_iat, daemon=True).start()
        threading.Thread(target=self._recv_iat_results, daemon=True).start()

    def feed_chunk(self, chunk: bytes):
        if self.closed: return
        try:
            if self._dump: self._dump.write(chunk)
            self._in_bytes += len(chunk)
            self.ffmpeg.stdin.write(chunk); self.ffmpeg.stdin.flush()
        except Exception:
            pass

    def _read_ffmpeg_pcm(self):
        frame = 1280
        buf = b""
        while not self.closed:
            data = self.ffmpeg.stdout.read(4096)
            if not data: time.sleep(0.005); continue
            buf += data
            while len(buf) >= frame:
                piece, buf = buf[:frame], buf[frame:]
                self._pcm_q.put(piece)

    def _pump_pcm_to_iat(self):
        # 等待首帧发完，避免 status=1 抢先
        while not self.closed and not self._ws_send_started:
            time.sleep(0.01)
        while not self.closed:
            try:
                pcm = self._pcm_q.get(timeout=0.1)
            except queue.Empty:
                continue
            payload = {
                "data":{
                    "status":1,
                    "format":"audio/L16;rate=16000",
                    "encoding":"raw",
                    "audio": base64.b64encode(pcm).decode()
                }
            }
            try:
                self.ws.send(json.dumps(payload))
            except Exception as e:
                print("[IAT] send error:", e)
                self.closed = True
                return

    def _recv_iat_results(self):
        try:
            while not self.closed:
                msg = self.ws.recv()
                if not msg: time.sleep(0.005); continue
                j = json.loads(msg)
                if j.get("code", 0) != 0:
                    print("[IAT] error:", j.get("code"), j.get("message"), "sid=", j.get("sid"))
                    try: self.ws.close()
                    except: pass
                    self.closed = True
                    return
                data = j.get("data", {})
                res  = data.get("result")
                if res and "ws" in res:
                    txt = "".join(w["w"] for blk in res["ws"] for w in blk["cw"])
                    if data.get("status") == 2:
                        self.on_final(txt)
                    else:
                        self.on_partial(txt)
        except Exception as e:
            print("[IAT] recv exception:", e)

    def stop(self):
        if self.closed: return
        self.closed = True
        try:
            self.ws.send(json.dumps({"data":{"status":2}}))
        except Exception:
            pass
        try: self.ws.close()
        except: pass
        try:
            self.ffmpeg.stdin.close(); self.ffmpeg.terminate()
        except Exception: pass
        if self._dump:
            try: self._dump.close()
            except: pass
        print(f"[IAT] total_in_bytes={self._in_bytes}, mime={self.mime}")

# ========= WS 路由 =========
@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type":"info","message":"connected"})

    loop = asyncio.get_running_loop()
    iat = None

    def send_json_threadsafe(obj: dict):
        try: asyncio.run_coroutine_threadsafe(ws.send_json(obj), loop)
        except Exception: pass

    def on_partial(text: str):
        send_json_threadsafe({"type":"partial","text":text})

    def on_final(text: str):
        send_json_threadsafe({"type":"final","text":text})
        q = (text or "").strip()
        if not q: return
        try:
            ans = call_llm(q) if ENABLE_LLM else mock_answer(q)
        except Exception as e:
            ans = f"（生成回答失败：{e}）"
        try:
            url = xunfei_tts(ans, vcn="x4_lingxia")
            send_json_threadsafe({"type":"tts","url":url})
        except Exception as e:
            send_json_threadsafe({"type":"error","message":f"TTS失败：{e}"})

    try:
        while True:
            raw = await ws.receive()
            if raw["type"] == "websocket.disconnect":
                break
            if "text" in raw:
                try:
                    msg = json.loads(raw["text"])
                except Exception:
                    continue
                if msg.get("type") == "start":
                    mime = msg.get("mime","")
                    await ws.send_json({"type":"info","message":f"start received, mime={mime}"})
                    iat = IATSession(on_partial, on_final, mime=mime)
                elif msg.get("type") == "stop":
                    if iat: iat.stop(); iat = None
                    await ws.send_json({"type":"info","message":"stopped"})
            elif "bytes" in raw:
                if iat: iat.feed_chunk(raw["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        try:
            if iat: iat.stop()
        except Exception:
            pass

@app.get("/health")
def health():
    asr_cfg = all([IAT_APP_ID, IAT_API_KEY, IAT_API_SECRET])
    tts_cfg = all([TTS_APP_ID, TTS_API_KEY, TTS_API_SECRET])
    return {"asr_ok": asr_cfg, "tts_ok": tts_cfg, "enable_llm": ENABLE_LLM, "openai_base": OPENAI_BASE, "model": OPENAI_MODEL}
