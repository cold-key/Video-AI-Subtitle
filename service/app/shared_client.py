"""Network and durable local storage for the opt-in private cache."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

from .config import APP_DIR
from .models import ServiceConfig
from .shared_schema import CacheRecord, MAX_BYTES, ResourceIdentity

SHARED_DIR = APP_DIR / "shared-cache"


class SharedError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:16]}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SharedClient:
    def __init__(self, config: ServiceConfig, transport=None):
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=config.shared_cache_url.rstrip("/") + "/", timeout=10,
            headers={"Authorization": f"Bearer {config.shared_cache_token}"},
            transport=transport, follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method: str, path: str, data=None):
        content = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        if content and len(content) > MAX_BYTES:
            raise SharedError("结果超过 32 MiB，已保留本机副本，未同步", 413)
        try:
            async with self.http.stream(method, path, content=content, headers={"Content-Type": "application/json"}) as response:
                if response.status_code >= 300:
                    raise SharedError({401: "共享服务令牌无效", 409: "共享任务占用已失效", 413: "结果超过 32 MiB，未同步"}.get(response.status_code, "共享服务暂不可用"), response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    # GET may contain a complete result plus a regeneration checkpoint.
                    if len(body) > MAX_BYTES * 2 + 65536:
                        raise SharedError("共享结果超过大小限制")
                return json.loads(body)
        except (httpx.HTTPError, ValueError) as exc:
            raise SharedError("无法连接共享服务或响应无效") from exc

    async def claim(self, identity: ResourceIdentity, owner: str, regenerate=False):
        return await self.request("POST", f"v1/cache/{identity.key}/claim", {"owner": owner, "regenerate": regenerate})

    async def lease_action(self, identity, lease, action):
        return await self.request("POST", f"v1/cache/{identity.key}/{action}", lease)

    async def submit(self, record, lease):
        return await self.request("PUT", f"v1/cache/{record.identity.key}", {**lease, "record": record.model_dump()})


def checked_record(data, identity, checksum=None):
    record = CacheRecord.model_validate(data)
    if record.identity != identity or (checksum is not None and record.checksum != checksum):
        raise SharedError("共享结果身份或校验值不匹配")
    return record


def any_local_record(identity):
    path = SHARED_DIR / "results" / f"{identity.key}.json"
    try:
        record = checked_record(json.loads(path.read_text(encoding="utf-8")), identity)
        return record if record.complete else None
    except (OSError, ValueError, SharedError):
        return None


def local_record(identity):
    record = any_local_record(identity)
    return record if record and record.timing_current else None


def preserve_stale_record(record):
    """Keep a local copy before a legacy timing record is replaced."""
    if record.timing_current:
        return
    path = SHARED_DIR / "results" / f"{record.identity.key}.timing-v{record.subtitle_timing_version}.json"
    if not path.exists():
        atomic_json(path, record.model_dump())


def save_record(record):
    atomic_json(SHARED_DIR / "results" / f"{record.identity.key}.json", record.model_dump())


def queue_record(record, config):
    # Queue metadata binds pending uploads to a server; changing accounts must
    # never silently publish old data to a newly configured destination.
    import hashlib
    destination = hashlib.sha256((config.shared_cache_url.rstrip("/") + "\n" + config.shared_cache_token).encode()).hexdigest()
    atomic_json(SHARED_DIR / "outbox" / f"{record.identity.key}.json", {
        "destination": destination, "record": record.model_dump(),
    })


def discard_queued(record):
    path = SHARED_DIR / "outbox" / f"{record.identity.key}.json"
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
        if pending.get("record") == record.model_dump():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


async def flush_outbox(config):
    import hashlib
    destination = hashlib.sha256((config.shared_cache_url.rstrip("/") + "\n" + config.shared_cache_token).encode()).hexdigest()
    client = SharedClient(config)
    try:
        for path in (SHARED_DIR / "outbox").glob("*.json"):
            try:
                pending = json.loads(path.read_text(encoding="utf-8"))
                if pending["destination"] != destination:
                    continue
                record = CacheRecord.model_validate(pending["record"])
                if not record.timing_current:
                    continue
                claim = await client.claim(record.identity, "outbox-" + uuid.uuid4().hex)
                if claim["state"] == "busy":
                    continue
                if claim["state"] == "acquired":
                    lease = {name: claim[name] for name in ("generation", "token")}
                    try:
                        await client.submit(record, lease)
                    finally:
                        try:
                            await client.lease_action(record.identity, lease, "release")
                        except SharedError:
                            pass
                # Avoid erasing a replacement queued while this request was in flight.
                if path.exists() and json.loads(path.read_text(encoding="utf-8")) == pending:
                    path.unlink()
            except (ValueError, OSError, SharedError):
                continue
    finally:
        await client.close()
