import os, time, uuid, logging, json, asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

# ---------- 配置 ----------
HEARTBEAT_INTERVAL_DEFAULT = int(os.getenv("HB_INTERVAL", "30"))       # 设备心跳建议间隔（秒）
HEARTBEAT_TTL_FACTOR = float(os.getenv("HB_TTL_FACTOR", "3.0"))         # TTL = 间隔 * 因子
TRUST_PROXY = os.getenv("TRUST_PROXY", "1") == "1"                      # 若服务后有反代，读取 XFF 头
REDIS_URL = os.getenv("REDIS_URL")                                      # e.g. redis://localhost:6379/0

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("app")

# ---------- 可选 Redis ----------
redis = None
try:
    # redis>=5 支持 asyncio 客户端
    import redis.asyncio as redis  # type: ignore
except Exception:
    redis = None

# ---------- 工具 ----------
def now_ts() -> float:
    return time.time()

def utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc).isoformat()

def client_ip_from(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        xrip = request.headers.get("x-real-ip")
        if xrip:
            return xrip.strip()
    return request.client.host if request.client else "unknown"

# ---------- 访问日志中间件 ----------
class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        ip = client_ip_from(request)
        ua = request.headers.get("user-agent", "-")
        path = request.url.path
        method = request.method
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            dur = (time.perf_counter() - t0) * 1000
            log.info(json.dumps({
                "type": "access",
                "ip": ip,
                "ua": ua,
                "method": method,
                "path": path,
                "status": locals().get("status", "NA"),
                "ms": round(dur, 2),
            }))

# ---------- 设备存储（内存回退 + Redis 优先） ----------
class DeviceStore:
    """抽象：保存设备注册、token、最后心跳时间、最近指标"""
    async def init(self): ...
    async def upsert_device(self, device_id: str, info: Dict[str, Any]): ...
    async def set_token(self, device_id: str, token: str): ...
    async def get_token(self, device_id: str) -> Optional[str]: ...
    async def heartbeat(self, device_id: str, hb: Dict[str, Any], ttl: int): ...
    async def list_devices(self) -> List[Dict[str, Any]]: ...

class MemoryStore(DeviceStore):
    def __init__(self):
        self._dev: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def init(self): ...

    async def upsert_device(self, device_id: str, info: Dict[str, Any]):
        async with self._lock:
            d = self._dev.setdefault(device_id, {})
            d.update(info)

    async def set_token(self, device_id: str, token: str):
        async with self._lock:
            d = self._dev.setdefault(device_id, {})
            d["token"] = token

    async def get_token(self, device_id: str) -> Optional[str]:
        async with self._lock:
            return self._dev.get(device_id, {}).get("token")

    async def heartbeat(self, device_id: str, hb: Dict[str, Any], ttl: int):
        async with self._lock:
            d = self._dev.setdefault(device_id, {})
            d.setdefault("meta", {})
            d["last_seen_ts"] = now_ts()
            d["last_seen"] = utc_iso()
            d["ttl"] = ttl
            d["metrics"] = hb.get("metrics")
            d["status"] = hb.get("status", "ok")

    async def list_devices(self) -> List[Dict[str, Any]]:
        async with self._lock:
            out = []
            for did, d in self._dev.items():
                last = d.get("last_seen_ts")
                ttl = int(d.get("ttl") or HEARTBEAT_INTERVAL_DEFAULT * HEARTBEAT_TTL_FACTOR)
                online = (now_ts() - (last or 0)) < ttl
                out.append({
                    "device_id": did,
                    "token_set": bool(d.get("token")),
                    "model": d.get("model"),
                    "fw": d.get("fw"),
                    "last_seen": d.get("last_seen"),
                    "seconds_ago": None if last is None else int(now_ts() - last),
                    "online": online,
                    "status": d.get("status", "-"),
                    "metrics": d.get("metrics"),
                })
            return out

class RedisStore(DeviceStore):
    def __init__(self, url: str):
        self._url = url
        self._r = None

    async def init(self):
        self._r = redis.from_url(self._url, decode_responses=True)

    async def upsert_device(self, device_id: str, info: Dict[str, Any]):
        await self._r.hset(f"dev:{device_id}", mapping=info)
        await self._r.sadd("devices", device_id)

    async def set_token(self, device_id: str, token: str):
        await self._r.hset(f"dev:{device_id}", "token", token)
        await self._r.sadd("devices", device_id)

    async def get_token(self, device_id: str) -> Optional[str]:
        return await self._r.hget(f"dev:{device_id}", "token")

    async def heartbeat(self, device_id: str, hb: Dict[str, Any], ttl: int):
        key = f"dev:{device_id}"
        mapping = {
            "last_seen_ts": str(now_ts()),
            "last_seen": utc_iso(),
            "ttl": str(ttl),
            "status": hb.get("status", "ok"),
            "metrics": json.dumps(hb.get("metrics", {})),
        }
        await self._r.hset(key, mapping=mapping)
        await self._r.sadd("devices", device_id)

    async def list_devices(self) -> List[Dict[str, Any]]:
        devs = []
        for did in await self._r.smembers("devices"):
            h = await self._r.hgetall(f"dev:{did}")
            last = float(h.get("last_seen_ts") or 0)
            ttl = int(float(h.get("ttl") or HEARTBEAT_INTERVAL_DEFAULT * HEARTBEAT_TTL_FACTOR))
            online = (now_ts() - last) < ttl if last else False
            metrics = None
            if "metrics" in h:
                try:
                    metrics = json.loads(h["metrics"])
                except Exception:
                    metrics = h["metrics"]
            devs.append({
                "device_id": did,
                "token_set": "token" in h,
                "model": h.get("model"),
                "fw": h.get("fw"),
                "last_seen": h.get("last_seen"),
                "seconds_ago": None if last == 0 else int(now_ts() - last),
                "online": online,
                "status": h.get("status", "-"),
                "metrics": metrics,
            })
        return devs

# ---------- 依赖注入 ----------
store: DeviceStore = MemoryStore()  # 默认内存；若配置了 REDIS_URL 且库可用，启动时替换

async def get_store() -> DeviceStore:
    return store

# ---------- 数据模型 ----------
class RegisterReq(BaseModel):
    device_id: str = Field(..., min_length=1)
    model: Optional[str] = None
    fw: Optional[str] = None
    token: Optional[str] = None  # 设备若自带 token 也可上报

class RegisterResp(BaseModel):
    device_id: str
    token: str
    next_heartbeat_in: int = HEARTBEAT_INTERVAL_DEFAULT

class HeartbeatReq(BaseModel):
    device_id: str
    token: str
    status: Optional[str] = "ok"
    metrics: Optional[Dict[str, Any]] = None
    suggested_interval: Optional[int] = None  # 设备也可请求新间隔

class HeartbeatResp(BaseModel):
    device_id: str
    accepted: bool
    next_heartbeat_in: int
    server_time: str

# ---------- 应用 ----------
app = FastAPI(title="Device Backend with Heartbeat")
app.add_middleware(AccessLogMiddleware)

@app.on_event("startup")
async def _startup():
    global store
    if REDIS_URL and redis is not None:
        try:
            rs = RedisStore(REDIS_URL)
            await rs.init()
            store = rs
            log.info("DeviceStore=Redis | url=%s", REDIS_URL)
        except Exception as e:
            log.warning("Redis init failed: %s, fallback to MemoryStore", e)
    else:
        log.info("DeviceStore=Memory")
    log.info(json.dumps({"type": "startup", "time": utc_iso()}))

@app.on_event("shutdown")
async def _shutdown():
    log.info(json.dumps({"type": "shutdown", "time": utc_iso()}))

# ---------- 小工具：简易鉴权 ----------
async def ensure_device_token(req: HeartbeatReq, st: DeviceStore):
    token_saved = await st.get_token(req.device_id)
    if not token_saved:
        raise HTTPException(401, "device not registered")
    if token_saved != req.token:
        raise HTTPException(403, "invalid token")

# ---------- 路由 ----------
@app.get("/whoami")
async def whoami(request: Request):
    return {
        "ip": client_ip_from(request),
        "ua": request.headers.get("user-agent"),
        "xff": request.headers.get("x-forwarded-for"),
        "time": utc_iso(),
    }

@app.post("/device/register", response_model=RegisterResp)
async def register(payload: RegisterReq, request: Request, st: DeviceStore = Depends(get_store)):
    ip = client_ip_from(request)
    token = payload.token or uuid.uuid4().hex
    await st.upsert_device(payload.device_id, {
        "model": payload.model,
        "fw": payload.fw,
        "registered_ip": ip,
        "registered_at": utc_iso(),
    })
    await st.set_token(payload.device_id, token)
    log.info(json.dumps({
        "type": "device_register",
        "device_id": payload.device_id,
        "ip": ip,
        "model": payload.model,
        "fw": payload.fw,
    }))
    return RegisterResp(device_id=payload.device_id, token=token, next_heartbeat_in=HEARTBEAT_INTERVAL_DEFAULT)

@app.post("/device/heartbeat", response_model=HeartbeatResp)
async def heartbeat(payload: HeartbeatReq, request: Request, st: DeviceStore = Depends(get_store)):
    await ensure_device_token(payload, st)
    interval = payload.suggested_interval or HEARTBEAT_INTERVAL_DEFAULT
    ttl = int(interval * HEARTBEAT_TTL_FACTOR)
    await st.heartbeat(payload.device_id, payload.dict(), ttl=ttl)

    log.info(json.dumps({
        "type": "heartbeat",
        "device_id": payload.device_id,
        "ip": client_ip_from(request),
        "status": payload.status,
        "interval": interval,
        "ttl": ttl,
    }))
    return HeartbeatResp(
        device_id=payload.device_id,
        accepted=True,
        next_heartbeat_in=interval,
        server_time=utc_iso(),
    )

@app.get("/devices/online")
async def devices_online(only_online: bool = Query(False), st: DeviceStore = Depends(get_store)):
    items = await st.list_devices()
    if only_online:
        items = [x for x in items if x.get("online")]
    # 排序：在线优先、最近心跳靠前
    items.sort(key=lambda x: (not x.get("online"), x.get("seconds_ago") or 10**9))
    return {"count": len(items), "items": items, "time": utc_iso()}

# ---------- WebSocket（可选） ----------
@app.websocket("/ws/{device_id}")
async def ws_device(ws: WebSocket, device_id: str, token: Optional[str] = Query(None), st: DeviceStore = Depends(get_store)):
    await ws.accept()
    # 简易鉴权（可选）
    saved = await st.get_token(device_id)
    if saved and token != saved:
        await ws.send_json({"error": "invalid token"})
        await ws.close(code=4403)
        return
    log.info(json.dumps({"type": "ws_open", "device_id": device_id, "time": utc_iso()}))
    try:
        while True:
            msg = await ws.receive_text()
            # 任何收到的消息都当做“活跃心跳”来记录
            await st.heartbeat(device_id, {"status": "ws", "metrics": {"msg": "pong"}}, ttl=int(HEARTBEAT_INTERVAL_DEFAULT * HEARTBEAT_TTL_FACTOR))
            await ws.send_text("ok")
    except WebSocketDisconnect:
        log.info(json.dumps({"type": "ws_close", "device_id": device_id, "time": utc_iso()}))
