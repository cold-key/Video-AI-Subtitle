from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import pipeline
from .config import load_config
from .models import ProcessedVideo, VideoRequest
from .shared_client import SHARED_DIR, SharedClient, SharedError, atomic_json, discard_queued, local_record, queue_record, save_record
from .shared_jobs import CAPTIONS, REQUESTS, SharedJobRequest, create_shared_job, make_record
from .shared_schema import ResourceIdentity

router = APIRouter(prefix="/shared")
IMPORT = {"state": "idle", "total": 0, "done": 0, "uploaded": 0, "existing": 0, "skipped": 0, "failed": 0, "details": []}
IMPORT_TASK = None
IMPORT_CANCEL = False


def require_enabled():
    config = load_config()
    if not config.shared_cache_enabled or not config.shared_cache_url or not config.shared_cache_token:
        raise HTTPException(400, "请先保存共享缓存地址及令牌并启用共享")
    return config


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("www.bilibili.com", "bilibili.com", "www.youtube.com", "youtube.com", "youtu.be"):
        raise HTTPException(400, "仅支持 B站或 YouTube 视频链接")


@router.post("/test")
async def connection_test():
    client = SharedClient(require_enabled())
    try:
        return await client.request("GET", "v1/status")
    except SharedError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        await client.close()


@router.get("/status")
def status():
    return {"pending": len(list((SHARED_DIR / "outbox").glob("*.json"))), "import": IMPORT}


def alias_path(url):
    import hashlib
    return SHARED_DIR / "aliases" / (hashlib.sha256(url.encode()).hexdigest() + ".json")


class AliasRequest(BaseModel):
    url: str


@router.post("/local-lookup")
def alias_lookup(request: AliasRequest):
    validate_url(request.url)
    try:
        identity = ResourceIdentity.model_validate_json(alias_path(request.url).read_text(encoding="utf-8"))
        if local_record(identity):
            return {"identity": identity.model_dump()}
    except (OSError, ValueError):
        pass
    return {"identity": None}


@router.post("/preflight")
async def preflight(request: SharedJobRequest):
    config = require_enabled()
    validate_url(request.url)
    if not request.regenerate and local_record(request.identity):
        return {"state": "complete", "origin": "local"}
    client = SharedClient(config)
    try:
        data = await client.request("GET", f"v1/cache/{request.identity.key}")
        return {"state": data["state"], "origin": "shared"}
    except SharedError:
        return {"state": "offline"}
    finally:
        await client.close()


@router.post("/jobs")
async def start(request: SharedJobRequest):
    require_enabled()
    validate_url(request.url)
    if pipeline.platform_from_url(request.url) != request.identity.platform:
        raise HTTPException(422, "视频平台与身份不匹配")
    if request.identity.platform == "youtube" and pipeline.video_id_from_url(request.url) != request.identity.video_id:
        raise HTTPException(422, "视频标识不匹配")
    atomic_json(alias_path(request.url), request.identity.model_dump())
    return create_shared_job(request)


@router.post("/jobs/{job_id}/subtitles")
def subtitles(job_id: str, request: VideoRequest):
    original = REQUESTS.get(job_id)
    job = pipeline.JOBS.get(job_id)
    if not original or not job or not job.needs_subtitles:
        raise HTTPException(409, "任务未请求字幕")
    identity = request.page_subtitle_identity
    if str(request.url) != original.url or not identity or identity.bvid != original.identity.video_id or identity.cid != original.identity.cid:
        raise HTTPException(409, "视频已切换或字幕身份不匹配")
    if (request.page_subtitles and request.page_subtitle_status != "found") or (not request.page_subtitles and request.page_subtitle_status != "no_tracks"):
        raise HTTPException(409, "尚未确认当前视频字幕状态")
    CAPTIONS[job_id] = request
    return {"ok": True}


async def resolve_legacy(url):
    validate_url(url)
    if pipeline.platform_from_url(url) == "youtube":
        return ResourceIdentity(platform="youtube", video_id=pipeline.video_id_from_url(url))
    parsed = urlparse(url)
    video_id = pipeline.video_id_from_url(url)
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        if video_id.startswith("ep"):
            response = await client.get("https://api.bilibili.com/pgc/view/web/season", params={"ep_id": video_id[2:]})
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or payload.get("result") or {}
            episode = next(item for item in data.get("episodes", []) if str(item["id"]) == video_id[2:])
            return ResourceIdentity(platform="bilibili", video_id=episode["bvid"], cid=episode["cid"])
        if video_id.startswith("BV"):
            params = {"bvid": video_id}
        elif video_id.startswith("av"):
            params = {"aid": video_id[2:]}
        else:
            raise ValueError("无法确认旧缓存的视频身份")
        response = await client.get("https://api.bilibili.com/x/web-interface/view", params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError("B站身份接口不可用")
        data = payload["data"]
        part = int(parse_qs(parsed.query).get("p", ["1"])[0])
        if part < 1:
            raise ValueError("无效分 P")
        return ResourceIdentity(platform="bilibili", video_id=data["bvid"], cid=data["pages"][part - 1]["cid"])


async def import_legacy():
    global IMPORT_CANCEL
    config = require_enabled()
    client = SharedClient(config)
    seen = set()
    files = sorted(pipeline.CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    IMPORT.update(state="running", total=len(files), done=0, uploaded=0, existing=0, skipped=0, failed=0, details=[])
    try:
        for path in files:
            if IMPORT_CANCEL:
                IMPORT["state"] = "cancelled"
                break
            reason = ""
            outcome = "skipped"
            try:
                if not path.name.endswith(".v8.json") or ".partial." in path.name or path.name.startswith("shared_"):
                    raise ValueError("非 v8 完整旧缓存")
                result = ProcessedVideo.model_validate_json(path.read_text(encoding="utf-8"))
                if result.video_id != pipeline.video_id_from_url(result.url) or result.platform != pipeline.platform_from_url(result.url):
                    raise ValueError("旧缓存身份不一致")
                identity = await resolve_legacy(result.url)
                cid = re.search(r"_cid(\d+)\.v8\.json$", path.name)
                if cid and int(cid[1]) != identity.cid:
                    raise ValueError("旧 CID 与当前资源不一致")
                record = make_record(identity, result.model_dump(), True, config)
                # Legacy generation settings are unknown, not today's settings.
                for name in ("translation_model", "summary_model", "whisper_model", "translation_prompt_hash", "summary_prompt_hash"):
                    setattr(record, name, "")
                if identity.key in seen:
                    raise ValueError("同一资源已有更新的有效文件")
                seen.add(identity.key)
                save_record(record)
                atomic_json(alias_path(result.url), identity.model_dump())
                queue_record(record, config)
                claim = await client.claim(identity, "import-" + identity.key)
                if claim["state"] == "complete":
                    outcome, reason = "existing", "共享端已存在"
                    discard_queued(record)
                elif claim["state"] == "busy":
                    queue_record(record, config)
                    reason = "其他设备正在处理，已加入待同步队列"
                else:
                    lease = {k: claim[k] for k in ("generation", "token")}
                    try:
                        await client.submit(record, lease)
                        outcome = "uploaded"
                        discard_queued(record)
                    finally:
                        with suppress(SharedError):
                            await client.lease_action(identity, lease, "release")
            except (ValueError, OSError, KeyError, IndexError, StopIteration, HTTPException) as exc:
                # Do not surface arbitrary external API messages or record text.
                safe_reasons = {"非 v8 完整旧缓存", "旧缓存身份不一致", "旧 CID 与当前资源不一致", "同一资源已有更新的有效文件"}
                reason = str(exc) if str(exc) in safe_reasons else "格式、完整性或视频身份校验未通过"
            except Exception:
                outcome, reason = "failed", "网络或共享服务错误，请重试"
            IMPORT[outcome] += 1
            IMPORT["done"] += 1
            IMPORT["details"].append({"file": path.name, "outcome": outcome, "reason": reason})
        else:
            IMPORT["state"] = "completed"
    finally:
        await client.close()


@router.post("/import")
async def start_import():
    global IMPORT_TASK, IMPORT_CANCEL
    require_enabled()
    if IMPORT_TASK and not IMPORT_TASK.done():
        raise HTTPException(409, "导入正在进行")
    IMPORT_CANCEL = False
    IMPORT_TASK = asyncio.create_task(import_legacy())
    return {"ok": True}


@router.post("/import/cancel")
def cancel_import():
    global IMPORT_CANCEL
    IMPORT_CANCEL = True
    return {"ok": True}
