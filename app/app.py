import os, ssl, hmac, json, base64, hashlib, asyncio, logging, uuid, datetime
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import quote
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from websocket import create_connection, WebSocketConnectionClosedException
import struct

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# --------- 环境与目录 ---------
ROOT = Path(__file__).parent.resolve()
WWW = ROOT / "static" / "www"; WWW.mkdir(parents=True, exist_ok=True)
TTS_DIR = ROOT / "static" / "tts"; TTS_DIR.mkdir(parents=True, exist_ok=True)
(WWW / "index.html").write_text("<h3>TCP Voice Gateway is running.</h3>", encoding="utf-8")

XF_APP_ID     = os.getenv("XF_APP_ID", "").strip()
XF_API_KEY    = os.getenv("XF_API_KEY", "").strip()
XF_API_SECRET = os.getenv("XF_API_SECRET", "").strip()
TTS_VCN       = os.getenv("TTS_VCN", "xiaoyan").strip()
TCP_HOST      = os.getenv("TCP_HOST", "0.0.0.0")
TCP_PORT      = int(os.getenv("TCP_PORT", "38001"))
assert XF_APP_ID and XF_API_KEY and XF_API_SECRET, "请配置 XF_APP_ID / XF_API_KEY / XF_API_SECRET"

# --------- Web 服务（用于暴露静态 mp3）---------
app = FastAPI(title="TCP Voice Gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

@app.get("/")
def index(): return FileResponse(str(WWW / "index.html"))
@app.get("/health")
def health(): return {"ok": True, "tcp": f"{TCP_HOST}:{TCP_PORT}"}

# --------- 协议常量（小端）---------
HD_SIZE = 32  # 8 * uint32
SOF = 0x000000AA  # 你给的注释“byte0=0xAA”，这里直接用 0xAA
CMD_SPEECH = 0x87     # 设备->服务器：语音帧(PCM 16k 16bit mono)
CMD_INFORM = 0x94     # 服务器->设备：播放URL通知
CMD_HEART  = 0x01     # 心跳(设备->服务器)，服务器回同 cmd
FMT_PCM16  = 0x01     # u32fmt=01

# --------- 打包/解包工具（小端）---------
def pack_header(sof, length, sendid, recvid, u32fmt, cmd, res, res2):
    return struct.pack("<8I", sof, length, sendid, recvid, u32fmt, cmd, res, res2)

def unpack_header(data: bytes):
    sof, length, sendid, recvid, u32fmt, cmd, res, res2 = struct.unpack("<8I", data)
    return {"sof": sof, "len": length, "sendid": sendid, "recvid": recvid,
            "fmt": u32fmt, "cmd": cmd, "res": res, "res2": res2}

# --------- 讯飞鉴权 ---------
def build_wss_url(host: str, path: str) -> str:
    date = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    signature_origin = f"host: {host}\n" f"date: {date}\n" f"GET {path} HTTP/1.1"
    digest = hmac.new(XF_API_SECRET.encode(), signature_origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    authorization_origin = (f'api_key="{XF_API_KEY}", algorithm="hmac-sha256", '
                            f'headers="host date request-line", signature="{signature}"')
    authorization = base64.b64encode(authorization_origin.encode()).decode()
    return f"wss://{host}{path}?authorization={quote(authorization)}&date={quote(date)}&host={host}"

# --------- IAT 会话（吃 PCM16k）---------
class IATSession:
    def __init__(self,
                 on_partial: Callable[[str], None],
                 on_final: Callable[[str], None]):
        self.closed = False
        self.on_partial = on_partial
        self.on_final = on_final
        url = build_wss_url("iat-api.xfyun.cn", "/v2/iat")
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
        first = {
            "common": {"app_id": XF_APP_ID},
            "business": {
                "language": "zh_cn", "domain": "iat", "accent": "mandarin",
                "vinfo": 1, "vad_eos": 3000
            },
            "data": {"status": 0, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""}
        }
        self.ws.send(json.dumps(first))
        self.t = asyncio.get_event_loop().run_in_executor(None, self._recv_loop)

    def _recv_loop(self):
        try:
            while not self.closed:
                msg = self.ws.recv()
                if not msg: continue
                data = json.loads(msg)
                if data.get("code", 0) != 0:
                    logging.warning("[IAT] err: %s", data); continue
                d = data.get("data", {})
                st = d.get("status")
                if "result" in d:
                    ws = d["result"].get("ws", [])
                    text = "".join([w["cw"][0]["w"] for w in ws if w.get("cw")])
                    if st in (0, 1):
                        self.on_partial(text)
                    elif st == 2:
                        self.on_final(text)
        except WebSocketConnectionClosedException:
            pass
        except Exception as e:
            logging.exception("[IAT] recv error: %s", e)

    def send_pcm(self, pcm: bytes, last: bool=False):
        if self.closed: return
        frame = {"data": {
            "status": 2 if last else 1, "format": "audio/L16;rate=16000",
            "encoding": "raw", "audio": base64.b64encode(pcm).decode()
        }}
        try: self.ws.send(json.dumps(frame))
        except Exception: self.closed = True

    def close(self):
        if self.closed: return
        try:
            self.send_pcm(b"", last=True)
            self.ws.close()
        finally:
            self.closed = True

# --------- TTS（WebSocket流式，返回mp3字节）---------
def tts_synth_to_mp3(text: str) -> bytes:
    url = build_wss_url("tts-api.xfyun.cn", "/v2/tts")
    ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
    first = {
        "common": {"app_id": XF_APP_ID},
        "business": {"aue": "lame", "vcn": TTS_VCN, "tte": "UTF8", "speed": 50, "pitch": 50, "volume": 50},
        "data": {"status": 2, "text": base64.b64encode(text.encode("utf-8")).decode("utf-8")}
    }
    ws.send(json.dumps(first))
    buf = bytearray()
    try:
        while True:
            resp = json.loads(ws.recv())
            if resp.get("code", 0) != 0:
                raise RuntimeError(f"TTS error: {resp}")
            d = resp.get("data", {})
            if "audio" in d:
                buf += base64.b64decode(d["audio"])
            if d.get("status") == 2:
                break
    finally:
        ws.close()
    return bytes(buf)

# --------- 会话状态 ---------
class ConnState:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.sendid = 0
        self.peer_last = 0
        self.iat: Optional[IATSession] = None
        self.partial = ""
        self.final = ""
        self.buffer = bytearray()

    def next_id(self):
        self.sendid = (self.sendid + 1) & 0xFFFFFFFF
        return self.sendid

# --------- 发送通知（URL）---------
async def send_inform_url(st: ConnState, url: str, record_after_play: int = 1):
    payload = url.encode("utf-8") + b"\x00"
    length = HD_SIZE + len(payload)
    hd = pack_header(SOF, length, st.next_id(), st.peer_last, FMT_PCM16, CMD_INFORM, record_after_play, 0)
    st.writer.write(hd + payload)
    await st.writer.drain()
    logging.info("-> inform url (%dB): %s", len(payload), url)

# --------- 发送心跳回包 ---------
async def send_heartbeat(st: ConnState):
    length = HD_SIZE
    hd = pack_header(SOF, length, st.next_id(), st.peer_last, FMT_PCM16, CMD_HEART, 0, 0)
    st.writer.write(hd)
    await st.writer.drain()

# --------- LLM 占位（你明天接入真正大模型时换掉）---------
def llm_reply(recognized_text: str) -> str:
    if not recognized_text.strip():
        return "抱歉，没有识别到有效语音。"
    return f"我听到你说：“{recognized_text}”。已收到～"

# --------- TCP 连接处理 ---------
async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    logging.info("TCP connected: %s", peer)
    st = ConnState(writer)

    try:
        buf = bytearray()
        while True:
            chunk = await reader.read(4096)
            if not chunk: break
            buf += chunk

            # 解析粘包
            while True:
                if len(buf) < HD_SIZE: break
                hd = unpack_header(buf[:HD_SIZE])
                if hd["sof"] != SOF:  # 丢弃到下一个可能的头
                    del buf[0:1]; continue
                total_len = hd["len"]
                if total_len < HD_SIZE or len(buf) < total_len: break

                frame = bytes(buf[:total_len]); del buf[:total_len]
                head = unpack_header(frame[:HD_SIZE])
                payload = frame[HD_SIZE:]
                st.peer_last = head["sendid"]

                cmd, res = head["cmd"], head["res"]

                if cmd == CMD_HEART:
                    # 心跳：立即回包
                    await send_heartbeat(st)
                    continue

                if cmd == CMD_SPEECH:
                    # 设备直吐 PCM16k/16bit/mono
                    if res == 0:  # 首帧
                        st.partial = ""; st.final = ""
                        if st.iat: st.iat.close()
                        st.iat = IATSession(
                            on_partial=lambda t: setattr(st, "partial", t),
                            on_final  =lambda t: setattr(st, "final", t)
                        )
                    if st.iat:
                        st.iat.send_pcm(payload, last=(res==2))

                    if res == 2:  # 尾帧 -> 收口，做 TTS 并下发 URL
                        await asyncio.sleep(0.25)
                        if st.iat:
                            st.iat.close(); st.iat = None
                        rec_text = st.final or st.partial
                        logging.info("[ASR] %s", rec_text)
                        # （这里接入你的大模型）
                        answer = llm_reply(rec_text)
                        # 讯飞 TTS 合成
                        try:
                            mp3 = await asyncio.get_event_loop().run_in_executor(None, tts_synth_to_mp3, answer)
                        except Exception as e:
                            logging.exception("TTS failed: %s", e)
                            continue
                        mp3_name = f"{uuid.uuid4().hex}.mp3"
                        (TTS_DIR / mp3_name).write_bytes(mp3)
                        url = f"/static/tts/{mp3_name}"
                        await send_inform_url(st, url, record_after_play=1)
                    continue

                # 未知命令：忽略
    except Exception as e:
        logging.exception("TCP error %s: %s", peer, e)
    finally:
        try:
            if st.iat: st.iat.close()
        except: pass
        writer.close()
        await writer.wait_closed()
        logging.info("TCP closed: %s", peer)

# --------- 启动 TCP server ---------
@app.on_event("startup")
async def _start_tcp():
    server = await asyncio.start_server(handle_client, TCP_HOST, TCP_PORT)
    app.state.tcp_server = server
    host, port = server.sockets[0].getsockname()
    logging.info("TCP server listening on %s:%s", host, port)

@app.on_event("shutdown")
async def _stop_tcp():
    srv = getattr(app.state, "tcp_server", None)
    if srv: srv.close(); await srv.wait_closed()
