import os
import ssl
import hmac
import json
import time
import base64
import hashlib
import asyncio
import logging
import threading
import subprocess
from typing import Callable, Optional
from urllib.parse import quote
from email.utils import formatdate

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
from websocket import create_connection, WebSocketConnectionClosedException

# ---------- 环境变量 ----------
XF_APP_ID     = os.getenv("XF_APP_ID", "")
XF_API_KEY    = os.getenv("XF_API_KEY", "")
XF_API_SECRET = os.getenv("XF_API_SECRET", "")
ENABLE_LLM    = os.getenv("ENABLE_LLM", "0") == "1"

assert XF_APP_ID and XF_API_KEY and XF_API_SECRET, \
    "请设置 XF_APP_ID / XF_API_KEY / XF_API_SECRET"

# ---------- FastAPI ----------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- 工具：讯飞鉴权 URL ----------
def build_xfyun_wss_url(host: str, path: str) -> str:
    """
    生成讯飞 WebSocket 鉴权 URL（HMAC-SHA256）
    host: 'iat-api.xfyun.cn' 或 'tts-api.xfyun.cn'
    path: '/v2/iat' 或 '/v2/tts'
    """
    date = formatdate(usegmt=True)  # RFC1123 GMT
    signature_origin = f"host: {host}\n" \
                       f"date: {date}\n" \
                       f"GET {path} HTTP/1.1"
    digest = hmac.new(
        XF_API_SECRET.encode("utf-8"),
        signature_origin.encode("utf-8"),
        hashlib.sha256
    ).digest()
    signature = base64.b64encode(digest).decode()

    authorization_origin = (
        f'api_key="{XF_API_KEY}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()
    return f"wss://{host}{path}?authorization={authorization}&date={quote(date)}&host={host}"

# ---------- 讯飞 IAT 会话 ----------
class IATSession:
    def __init__(self, on_partial: Callable[[str], None], on_final: Callable[[str], None]):
        self.on_partial = on_partial
        self.on_final = on_final
        self.closed = False

        url = build_xfyun_wss_url("iat-api.xfyun.cn", "/v2/iat")
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
        # 首帧（status=0）
        first = {
            "common": {"app_id": XF_APP_ID},
            "business": {
                "language": "zh_cn",
                "domain":   "iat",
                "accent":   "mandarin",
                "vad_eos":  5000
            },
            "data": {
                "status": 0,
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "audio": ""
            }
        }
        self.ws.send(json.dumps(first))
        # 接收线程
        self.t = threading.Thread(target=self._recv_loop, daemon=True)
        self.t.start()

    def _recv_loop(self):
        try:
            while not self.closed:
                msg = self.ws.recv()
                if not msg:
                    continue
                data = json.loads(msg)
                # err_no 非 0 为错误
                if data.get("code", 0) != 0:
                    logging.warning(f"[IAT] error: {data}")
                    continue
                result = data.get("data", {})
                status = result.get("status")
                # 解析中间结果
                if "result" in result:
                    ws = result["result"].get("ws", [])
                    text = "".join([w["cw"][0]["w"] for w in ws if w.get("cw")])
                    if status == 0 or status == 1:
                        self.on_partial(text)
                    elif status == 2:
                        self.on_final(text)
        except WebSocketConnectionClosedException:
            pass
        except Exception as e:
            logging.exception(f"[IAT] recv error: {e}")

    def send_pcm(self, pcm: bytes, is_last: bool = False):
        if self.closed:
            return
        frame = {
            "data": {
                "status": 2 if is_last else 1,
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "audio": base64.b64encode(pcm).decode()
            }
        }
        try:
            self.ws.send(json.dumps(frame))
        except Exception:
            self.closed = True

    def close(self):
        if self.closed:
            return
        try:
            # 发送最后一帧（status=2，无音频也可）
            self.send_pcm(b"", is_last=True)
            self.ws.close()
        finally:
            self.closed = True

# ---------- 讯飞 TTS 会话（一次性合成） ----------
class TTSSession:
    def synth(self, text: str, vcn: str = "x4_xiaoyan") -> bytes:
        url = build_xfyun_wss_url("tts-api.xfyun.cn", "/v2/tts")
        ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
        # 首帧发送整段文本，status=2
        first = {
            "common": {"app_id": XF_APP_ID},
            "business": {
                "aue": "lame",  # mp3
                "vcn": vcn,
                "tte": "UTF8",
                "speed": 50, "pitch": 50, "volume": 50
            },
            "data": {
                "status": 2,
                "text": base64.b64encode(text.encode("utf-8")).decode()
            }
        }
        ws.send(json.dumps(first))
        audio_mp3 = bytearray()
        try:
            while True:
                resp = json.loads(ws.recv())
                if resp.get("code", 0) != 0:
                    raise RuntimeError(f"TTS error: {resp}")
                data = resp.get("data", {})
                if "audio" in data:
                    audio_mp3 += base64.b64decode(data["audio"])
                if data.get("status") == 2:
                    break
        finally:
            ws.close()
        return bytes(audio_mp3)

# ---------- 工具：把 webm/opus 转成 16k PCM ----------
class OpusToPcm:
    """
    用 ffmpeg 将浏览器 MediaRecorder 的 webm/opus（或 ogg/opus）
    转成 s16le 16k 1ch 流。
    """
    def __init__(self, mime: str):
        # 让探测尽量快，适合流式
        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "webm" if "webm" in mime else "ogg",
                "-fflags", "+genpts",
                "-use_wallclock_as_timestamps", "1",
                "-i", "pipe:0",
                "-ac", "1",
                "-ar", "16000",
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0
        )
        self.stdout = self.proc.stdout
        self.stdin = self.proc.stdin
        self.closed = False

    def write(self, chunk: bytes):
        if not self.closed:
            try:
                self.stdin.write(chunk)
                self.stdin.flush()
            except Exception:
                self.closed = True

    def read(self, n=3200) -> bytes:
        # 3200 字节 ≈ 100ms（16000 * 2 bytes * 0.1s）
        return self.stdout.read(n)

    def close(self):
        if self.closed:
            return
        try:
            if self.stdin:
                self.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.closed = True

# ---------- API ----------
@app.get("/health")
def health():
    return {
        "asr_ok": True,
        "tts_ok": True,
        "enable_llm": ENABLE_LLM,
        "openai_base": os.getenv("OPENAI_BASE", "http://127.0.0.1:11434/v1"),
        "model": os.getenv("OPENAI_MODEL", "llama3.1")
    }

@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    await safe_send(ws, {"type": "info", "message": "connected"})
    trans: Optional[OpusToPcm] = None
    iat: Optional[IATSession] = None
    stop_flag = False

    # 回调：实时/最终文本 -> 回推前端，并触发 TTS
    def on_partial(text: str):
        asyncio.run_coroutine_threadsafe(
            safe_send(ws, {"type": "partial", "text": text}), asyncio.get_event_loop()
        )

    def on_final(text: str):
        asyncio.run_coroutine_threadsafe(
            safe_send(ws, {"type": "final", "text": text}), asyncio.get_event_loop()
        )
        # 也来一句 TTS（可关）
        try:
            tts = TTSSession()
            mp3 = tts.synth(text or "好的")
            # 写入静态目录，回放
            os.makedirs("/app/static/tts", exist_ok=True)
            key = f"{int(time.time()*1000)}.mp3"
            path = f"/app/static/tts/{key}"
            with open(path, "wb") as f:
                f.write(mp3)
            url = f"/static/tts/{key}"
            asyncio.run_coroutine_threadsafe(
                safe_send(ws, {"type": "tts", "url": url}), asyncio.get_event_loop()
            )
        except Exception as e:
            logging.warning(f"TTS error: {e}")

    try:
        # 1) 等待前端 start 帧，得到 mime
        first = await ws.receive_text()
        msg = json.loads(first)
        if msg.get("type") != "start":
            await safe_send(ws, {"type": "error", "message": "expect start"})
            await ws.close()
            return
        mime = msg.get("mime", "")
        await safe_send(ws, {"type": "info", "message": f"start received, mime={mime}"})

        # 2) 准备转码与 IAT
        trans = OpusToPcm(mime or "audio/webm;codecs=opus")
        iat = IATSession(on_partial, on_final)

        # 3) 异步读 PCM -> 推给 IAT
        async def pump_pcm():
            try:
                while not stop_flag:
                    pcm = trans.read(3200)
                    if not pcm:
                        await asyncio.sleep(0.01)
                        continue
                    iat.send_pcm(pcm, is_last=False)
            except Exception as e:
                logging.warning(f"pump_pcm error: {e}")

        pcm_task = asyncio.create_task(pump_pcm())

        # 4) 循环收前端的音频分片
        while True:
            event = await ws.receive()
            if "bytes" in event and event["bytes"] is not None:
                trans.write(event["bytes"])
            elif "text" in event and event["text"] is not None:
                data = json.loads(event["text"])
                if data.get("type") == "stop":
                    stop_flag = True
                    await safe_send(ws, {"type": "info", "message": "stopping"})
                    break
            else:
                await asyncio.sleep(0.005)

        # 5) 结束：送最后一帧
        await asyncio.sleep(0.3)  # 给 ffmpeg 吐完缓冲
        if iat:
            iat.send_pcm(b"", is_last=True)
        await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.exception(f"/ws/voice error: {e}")
        await safe_send(ws, {"type": "error", "message": str(e)})
    finally:
        try:
            if trans: trans.close()
        except Exception:
            pass
        try:
            if iat: iat.close()
        except Exception:
            pass
        if ws.application_state != WebSocketState.DISCONNECTED:
            await ws.close()

async def safe_send(ws: WebSocket, obj: dict):
    try:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass
