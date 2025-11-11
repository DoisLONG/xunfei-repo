import os, ssl, hmac, json, time, base64, hashlib, asyncio, logging, threading, subprocess, uuid
from typing import Callable, Optional
from urllib.parse import quote, urlencode
from email.utils import formatdate
from pathlib import Path

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketState
from websocket import create_connection, WebSocketConnectionClosedException

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

ROOT = Path(__file__).parent.resolve()
WWW_DIR = ROOT / "static" / "www"
TTS_DIR = ROOT / "static" / "tts"
WWW_DIR.mkdir(parents=True, exist_ok=True)
TTS_DIR.mkdir(parents=True, exist_ok=True)
(WWW_DIR / "index.html").touch(exist_ok=True)  # 防 404

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

@app.get("/")
def index():
    return FileResponse(str(WWW_DIR / "index.html"))

# ===== 讯飞三元组（请保证开通 IAT 与 语音合成 WebAPI v2）=====
XF_APP_ID     = os.getenv("XF_APP_ID", "").strip()     or "your_appid"
XF_API_KEY    = os.getenv("XF_API_KEY", "").strip()    or "your_apikey"
XF_API_SECRET = os.getenv("XF_API_SECRET", "").strip() or "your_apisecret"

def _assert_cfg():
    assert XF_APP_ID and XF_API_KEY and XF_API_SECRET, "请配置 XF_APP_ID/XF_API_KEY/XF_API_SECRET 环境变量，并开通相应服务"
_assert_cfg()

# ===== 工具：WS/IAT 鉴权（GET request-line）、TTS 鉴权（POST request-line）=====
def build_xfyun_wss_url(host: str, path: str) -> str:
    """IAT 使用 wss，签名 request-line 为 GET"""
    date = formatdate(usegmt=True)
    origin = f"host: {host}\n" f"date: {date}\n" f"GET {path} HTTP/1.1"
    sign = hmac.new(XF_API_SECRET.encode(), origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(sign).decode()
    auth_origin = (
        f'api_key="{XF_API_KEY}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(auth_origin.encode()).decode()
    return f"wss://{host}{path}?authorization={authorization}&date={quote(date)}&host={host}"

def build_xfyun_http_url(host: str, path: str, method: str = "POST") -> str:
    """TTS v2 使用 HTTPS，签名 request-line 必须与实际 method 一致（POST）"""
    date = formatdate(usegmt=True)
    origin = f"host: {host}\n" f"date: {date}\n" f"{method} {path} HTTP/1.1"
    sign = hmac.new(XF_API_SECRET.encode(), origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(sign).decode()
    auth_origin = (
        f'api_key="{XF_API_KEY}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(auth_origin.encode()).decode()
    return f"https://{host}{path}?{urlencode({'authorization': authorization, 'date': date, 'host': host})}"

# ===== 讯飞 IAT 会话（WS）=====
class IATSession:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 on_partial: Callable[[str], None],
                 on_final: Callable[[str], None]):
        self.loop = loop
        self.on_partial = on_partial
        self.on_final = on_final
        self.closed = False
        url = build_xfyun_wss_url("iat-api.xfyun.cn", "/v2/iat")
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})

        first = {
            "common": {"app_id": XF_APP_ID},
            "business": {
                "language": "zh_cn", "domain": "iat", "accent": "mandarin",
                "vad_eos": 3000
            },
            "data": {"status": 0, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""}
        }
        self.ws.send(json.dumps(first))
        self.t = threading.Thread(target=self._recv_loop, daemon=True)
        self.t.start()

    def _recv_loop(self):
        try:
            while not self.closed:
                msg = self.ws.recv()
                if not msg:
                    continue
                data = json.loads(msg)
                if data.get("code", 0) != 0:
                    logging.warning("[IAT] error: %s", data)
                    continue
                result = data.get("data", {})
                status = result.get("status")
                if "result" in result:
                    ws = result["result"].get("ws", [])
                    text = "".join([w["cw"][0]["w"] for w in ws if w.get("cw")])
                    if status in (0, 1):
                        self.loop.call_soon_threadsafe(self.on_partial, text)
                    elif status == 2:
                        self.loop.call_soon_threadsafe(self.on_final, text)
        except WebSocketConnectionClosedException:
            pass
        except Exception as e:
            logging.exception("[IAT] recv error: %s", e)

    def send_pcm(self, pcm: bytes, is_last: bool = False):
        if self.closed:
            return
        frame = {"data": {
            "status": 2 if is_last else 1,
            "format": "audio/L16;rate=16000",
            "encoding": "raw",
            "audio": base64.b64encode(pcm).decode()
        }}
        try:
            self.ws.send(json.dumps(frame))
        except Exception:
            self.closed = True

    def close(self):
        if self.closed: 
            return
        try:
            self.send_pcm(b"", is_last=True)
            self.ws.close()
        finally:
            self.closed = True

# ===== 讯飞 TTS（HTTPS POST，返回 mp3 bytes）=====
# ===== 讯飞 TTS（WebSocket v2，返回 mp3 bytes）=====
def xfyun_tts_bytes(text: str, vcn: str = "xiaoyan", aue: str = "lame") -> bytes:
    """
    适配“在线语音合成（流式版）”——必须走 WebSocket:
    端点: wss://tts-api.xfyun.cn/v2/tts
    鉴权: HMAC + GET request-line（与 IAT 相同）
    aue="lame" -> mp3; 也可 "raw" / "speex" 等
    """
    url = build_xfyun_wss_url("tts-api.xfyun.cn", "/v2/tts")
    ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})

    try:
        first = {
            "common": {"app_id": XF_APP_ID},
            "business": {
                "aue": aue,        # mp3
                "vcn": vcn,        # 例：xiaoyan；若开通 x4 系列，可用 x4_xiaoyan
                "tte": "UTF8",
                "speed": 50,
                "volume": 50,
                "pitch": 50,
                "sfl": 1           # 流式返回
            },
            "data": {
                "status": 2,       # 一次性提交文本，直接结束
                "text": base64.b64encode(text.encode("utf-8")).decode("utf-8")
            }
        }
        ws.send(json.dumps(first))

        audio = bytearray()
        sid = None

        while True:
            msg = ws.recv()
            if not msg:
                break
            resp = json.loads(msg)
            code = resp.get("code", 0)
            sid = resp.get("sid")
            if code != 0:
                raise RuntimeError(f"XF TTS error code={code}, msg={resp.get('message')}, sid={sid}")

            data = resp.get("data", {})
            if "audio" in data:
                audio.extend(base64.b64decode(data["audio"]))

            if data.get("status") == 2:  # 结束
                break

        if not audio:
            raise RuntimeError(f"XF TTS empty audio, sid={sid}")

        return bytes(audio)

    finally:
        try:
            ws.close()
        except Exception:
            pass

# ===== webm/opus → 16k PCM（ffmpeg）=====
class OpusToPcm:
    def __init__(self, mime: str):
        self.proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "webm" if "webm" in mime.lower() else "ogg",
                "-fflags", "+genpts",
                "-use_wallclock_as_timestamps", "1",
                "-i", "pipe:0",
                "-ac", "1", "-ar", "16000",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "pipe:1",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
        )
        self.stdin = self.proc.stdin
        self.stdout = self.proc.stdout

    def close(self):
        try:
            if self.stdin and not self.stdin.closed:
                self.stdin.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

# ===== 健康检查 =====
@app.get("/health")
def health():
    return {"ok": True, "xf_app": XF_APP_ID[:4] + "..." + XF_APP_ID[-4:], "tts_dir": str(TTS_DIR)}

# ===== WebSocket：语音上行 + IAT 转写 + 自动 TTS 回推 =====
@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()

    async def post(obj: dict):
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))

    await post({"type": "info", "message": "connected"})

    trans: Optional[OpusToPcm] = None
    iat: Optional[IATSession] = None
    mime = "audio/webm;codecs=opus"
    stop_flag = False
    asr_full_text = ""

    # WS 分片 → ffmpeg.stdin
    q_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
    def _append_final(t: str):
        nonlocal asr_full_text
        asr_full_text += t
        asyncio.run_coroutine_threadsafe(post({"type": "final", "text": t}), loop)

    async def writer():
        nonlocal trans
        try:
            while not stop_flag:
                chunk = await q_in.get()
                if trans and trans.stdin:
                    try:
                        trans.stdin.write(chunk)
                        trans.stdin.flush()
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    async def pump_pcm():
        """ffmpeg.stdout → 讯飞"""
        try:
            while not stop_flag and trans and trans.stdout:
                pcm = await loop.run_in_executor(None, trans.stdout.read, 3200)
                if not pcm:
                    await asyncio.sleep(0.01)
                    continue
                if iat:
                    iat.send_pcm(pcm, is_last=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.warning("pump_pcm error: %s", e)

    writer_task: Optional[asyncio.Task] = None
    pump_task: Optional[asyncio.Task] = None

    async def ensure_pipeline():
        nonlocal trans, iat, writer_task, pump_task
        if trans is None:
            trans = OpusToPcm(mime)
            logging.info("Pipeline created (mime=%s)", mime)
            iat = IATSession(
                loop,
                on_partial=lambda t: asyncio.run_coroutine_threadsafe(
                    post({"type": "partial", "text": t}), loop
                ),
                on_final=_append_final,
            )
            writer_task = asyncio.create_task(writer())
            pump_task = asyncio.create_task(pump_pcm())

    async def safe_receive():
        try:
            return await ws.receive()
        except RuntimeError as e:
            if "disconnect message" in str(e):
                return {"type": "disconnect"}
            raise
        except asyncio.CancelledError:
            return {"type": "cancel"}

    logging.info("WS connected, waiting frames...")

    try:
        # 可能的 start 文本帧
        first = await safe_receive()
        if first.get("text"):
            try:
                m = json.loads(first["text"])
                if m.get("type") == "start":
                    mime = m.get("mime") or mime
                    await post({"type": "info", "message": f"start received, mime={mime}"})
            except Exception:
                pass
        elif first.get("bytes"):
            await ensure_pipeline()
            await q_in.put(first["bytes"])

        # —— 主循环 —— #
        while True:
            event = await safe_receive()
            t = event.get("type")
            if t in ("disconnect", "cancel"):
                break

            if event.get("bytes") is not None:
                b = event["bytes"]
                if not b:
                    continue
                if trans is None:
                    await ensure_pipeline()
                await q_in.put(b)

            elif event.get("text") is not None:
                try:
                    data = json.loads(event["text"])
                except Exception:
                    continue
                if data.get("type") == "stop":
                    stop_flag = True
                    await post({"type": "info", "message": "stopping"})
                    break

        # flush & last frame
        await asyncio.sleep(0.30)
        if iat:
            iat.send_pcm(b"", is_last=True)
        await asyncio.sleep(0.25)

        # —— 模拟大模型回答 + 自动 TTS —— #
        reply = f"我听到了：“{asr_full_text.strip()}”。这是模拟回答：好的，已收到～"
        await post({"type": "llm", "text": reply})
        try:
            mp3 = xfyun_tts_bytes(reply, vcn="xiaoyan")
            name = f"{uuid.uuid4().hex}.mp3"
            out = TTS_DIR / name
            out.write_bytes(mp3)
            await post({"type": "tts", "url": f"/static/tts/{name}"})
            await post({"type": "info", "message": f"TTS ok, size={len(mp3)} bytes"})
        except Exception as e:
            await post({"type": "error", "message": f"TTS失败: {e}"})

    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.exception("/ws/voice error: %s", e)
        await post({"type": "error", "message": str(e)})
    finally:
        stop_flag = True
        for task in (writer_task, pump_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        try:
            if trans:
                trans.close()
        except Exception:
            pass
        try:
            if iat:
                iat.close()
        except Exception:
            pass
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.close()

# ===== 可选：REST 直呼 TTS（被“模拟回答”按钮使用）=====
@app.get("/api/tts")
def api_tts(text: str):
    try:
        b = xfyun_tts_bytes(text)
        name = f"{uuid.uuid4().hex}.mp3"
        (TTS_DIR / name).write_bytes(b)
        return {"ok": True, "url": f"/static/tts/{name}", "size": len(b)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
