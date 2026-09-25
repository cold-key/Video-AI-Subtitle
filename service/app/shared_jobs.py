"""Shared-job orchestration. The standalone server never downloads media."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import threading
import uuid
from contextlib import suppress

from pydantic import BaseModel

from . import pipeline
from .config import load_config
from .models import JobView, VideoRequest
from .shared_client import (
    SharedClient, SharedError, atomic_json, checked_record,
    any_local_record, discard_queued, flush_outbox, local_record,
    preserve_stale_record, queue_record, save_record,
)
from .shared_schema import CacheRecord, ResourceIdentity


class SharedJobRequest(BaseModel):
    url: str
    identity: ResourceIdentity
    local_only: bool = False
    regenerate: bool = False


class Interrupted(Exception):
    pass


CAPTIONS: dict[str, VideoRequest] = {}
REQUESTS: dict[str, SharedJobRequest] = {}


def finish(job, record, origin):
    job.result = record.result(REQUESTS[job.id].url)
    job.state, job.stage, job.progress = "completed", f"已从{origin}缓存加载", 100
    job.cache_origin = origin
    job.shared_state = "cached"
    job.preview_segments = job.result.segments
    job.total_segments = job.translated_segments = len(job.result.segments)
    job.summary_state, job.summary_partial = "completed", record.summary
    job.platform, job.source, job.source_language = record.identity.platform, record.source, record.source_language
    job.needs_subtitles = False


def make_record(identity, data, complete, config):
    from .prompts import load_prompt
    fields = {k: data[k] for k in (
        "title", "duration", "audio_duration", "subtitle_timing_version", "source",
        "source_language", "segments", "summary_partial", "summary_state", "summary", "key_points",
    ) if k in data}
    if complete:
        fields["summary_state"] = "completed"
    return CacheRecord(identity=identity, complete=complete, **fields,
        translation_model=config.translation_model, summary_model=config.summary_model,
        whisper_model=config.whisper_model,
        translation_prompt_hash=hashlib.sha256(load_prompt("translation").encode()).hexdigest(),
        summary_prompt_hash=hashlib.sha256(load_prompt("summary").encode()).hexdigest())


def promote_legacy(request, config, partial_path):
    """Reuse only v8 files matching this URL/part and, where present, CID."""
    key = pipeline.cache_key_from_url(request.url)
    keys = [key]
    if request.identity.platform == "bilibili":
        keys.append(f"{key}_cid{request.identity.cid}")
    candidates = [pipeline.CACHE_DIR / f"{value}.v8.json" for value in keys]
    candidates = sorted((p for p in candidates if p.exists()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if pipeline.cache_key_from_url(data["url"]) != key:
                continue
            record = make_record(request.identity, data, True, config)
            if not record.timing_current:
                continue
            for field in ("translation_model", "summary_model", "whisper_model", "translation_prompt_hash", "summary_prompt_hash"):
                setattr(record, field, "")
            save_record(record)
            return record
        except (ValueError, OSError, KeyError):
            continue
    if not partial_path.exists():
        for value in reversed(keys):
            path = pipeline.CACHE_DIR / f"{value}.partial.v8.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    request.identity.platform == "bilibili"
                    and data.get("source") == "whisper"
                    and data.get("subtitle_timing_version", 0) < pipeline.BILIBILI_WHISPER_TIMING_VERSION
                ):
                    continue
                if data.get("extraction_state", "completed") == "completed":
                    partial_record = make_record(request.identity, data, False, config)
                    if not partial_record.timing_current:
                        continue
                atomic_json(partial_path, data)
                break
            except (ValueError, OSError, KeyError):
                continue
    return None


def create_shared_job(request):
    job_id = uuid.uuid4().hex
    job = JobView(id=job_id, state="queued", stage="正在检查共享缓存", progress=0, shared_state="checking")
    pipeline.JOBS[job_id] = job
    pipeline.JOB_CONTROLS[job_id] = pipeline.JobControl()
    REQUESTS[job_id] = request
    pipeline.JOB_TASKS[job_id] = asyncio.create_task(run_shared_job(job_id))
    return job


async def run_shared_job(job_id):
    request = REQUESTS[job_id]
    config = load_config()
    job, control = pipeline.JOBS[job_id], pipeline.JOB_CONTROLS[job_id]
    client = SharedClient(config)
    lease = None
    monitor = None
    interrupted = False
    last_snapshot = ""
    last_summary_sync = 0.0
    terminal_state = None
    existing_local_record = any_local_record(request.identity)
    if existing_local_record and not existing_local_record.timing_current:
        preserve_stale_record(existing_local_record)
    # Regeneration uses a separate work key so the old complete result survives.
    # The first 128 identity-hash bits are ample for local filenames and keep them MAX_PATH-safe.
    local_identity_key = request.identity.key[:32]
    work_key = f"shared_{local_identity_key}"
    if request.identity.platform == "bilibili":
        work_key = f"shared_v{pipeline.BILIBILI_WHISPER_TIMING_VERSION}_{local_identity_key}"
    if request.regenerate:
        regen_key = job_id[:12] if request.identity.platform == "bilibili" else job_id
        work_key += f"_regen_{regen_key}"
    partial = pipeline.CACHE_DIR / f"{work_key}.partial.v8.json"
    result_path = pipeline.CACHE_DIR / f"{work_key}.v8.json"

    async def guard():
        if control.cancelled:
            raise asyncio.CancelledError
        if interrupted or not control.resume_event.is_set() or (control.shared_stop_event and control.shared_stop_event.is_set()):
            raise Interrupted("共享占用已释放，等待重新申请")

    async def wait_for_state(seconds):
        control.state_event.clear()
        if control.cancelled or not control.resume_event.is_set():
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(control.state_event.wait(), seconds)

    async def sync_checkpoint(force=False):
        nonlocal last_snapshot, last_summary_sync
        if not lease or not partial.exists():
            return
        data = json.loads(partial.read_text(encoding="utf-8"))
        if data.get("extraction_state", "completed") != "completed":
            return
        record = make_record(request.identity, data, False, config)
        # Translated batches sync immediately; streaming summary is throttled.
        snapshot = json.dumps(data.get("segments", []), ensure_ascii=False)
        if not force and snapshot == last_snapshot and time.monotonic() - last_summary_sync < 5:
            return
        await client.submit(record, lease)
        last_snapshot, last_summary_sync = snapshot, time.monotonic()

    async def release():
        nonlocal lease
        if lease:
            with suppress(SharedError, ValueError, OSError):
                await sync_checkpoint(True)
            with suppress(SharedError):
                await client.lease_action(request.identity, lease, "release")
            lease = None

    async def watch():
        nonlocal interrupted
        renewed = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            if control.cancelled or not control.resume_event.is_set():
                interrupted = True
                control.shared_stop_event.set()
                job.needs_subtitles = False
                await release()
                return
            try:
                if time.monotonic() - renewed >= 20:
                    await client.lease_action(request.identity, lease, "renew")
                    renewed = time.monotonic()
                await sync_checkpoint()
            except SharedError as exc:
                job.sync_error = str(exc)
                if exc.status == 413:
                    continue
                interrupted = True
                control.shared_stop_event.set()
                return
            except (ValueError, OSError):
                # Atomic checkpoints may not exist until extraction has finished.
                continue

    try:
        if not request.regenerate:
            record = local_record(request.identity) or promote_legacy(request, config, partial)
            if record:
                finish(job, record, "本机")
                queue_record(record, config)
                return
        delay = 3
        while True:
            control.shared_guard = None
            await control.checkpoint()
            job.state = "running"
            interrupted = False
            control.shared_stop_event = threading.Event()
            if not request.local_only:
                try:
                    claim = await client.claim(request.identity, job_id, request.regenerate)
                    delay = 3
                except SharedError as exc:
                    job.stage, job.shared_state, job.sync_error = "等待共享服务恢复", "offline", str(exc)
                    await wait_for_state(delay)
                    delay = min(30, delay * 2)
                    continue
                if claim["state"] == "complete":
                    record = checked_record(claim["record"], request.identity, claim["checksum"])
                    if not record.timing_current:
                        preserve_stale_record(record)
                        request.regenerate = True
                        work_key = f"shared_{local_identity_key}"
                        if request.identity.platform == "bilibili":
                            work_key = f"shared_v{pipeline.BILIBILI_WHISPER_TIMING_VERSION}_{local_identity_key}"
                        work_key += f"_regen_{job_id[:12]}"
                        partial = pipeline.CACHE_DIR / f"{work_key}.partial.v8.json"
                        result_path = pipeline.CACHE_DIR / f"{work_key}.v8.json"
                        job.stage, job.shared_state = "正在重新生成旧版 B 站语音时间戳", "processing"
                        continue
                    save_record(record)
                    finish(job, record, "共享")
                    return
                if claim["state"] == "busy":
                    job.stage, job.shared_state = "其他设备正在处理", "waiting"
                    await wait_for_state(3)
                    continue
                lease = {name: claim[name] for name in ("generation", "token")}
                if claim.get("checkpoint"):
                    record = checked_record(claim["checkpoint"], request.identity)
                    if record.timing_current:
                        atomic_json(partial, record.checkpoint())
                    else:
                        job.stage = "重新识别旧版 B 站语音时间戳"
                monitor = asyncio.create_task(watch())
            control.shared_guard = guard
            job.shared_state, job.sync_error = "processing", ""
            try:
                # Only the owner needs browser captions. Checkpoints bypass this.
                extraction_done = False
                if partial.exists():
                    data = json.loads(partial.read_text(encoding="utf-8"))
                    extraction_done = bool(data.get("segments")) and data.get("extraction_state", "completed") == "completed"
                if request.identity.platform == "bilibili" and not extraction_done and not result_path.exists():
                    job.needs_subtitles = True
                    job.stage = "正在读取当前视频字幕"
                    caption_deadline = time.monotonic() + 90
                    while job_id not in CAPTIONS:
                        await guard()
                        if time.monotonic() >= caption_deadline:
                            raise SharedError("页面未返回字幕信息，请在视频页面重试")
                        await asyncio.sleep(0.2)
                    job.needs_subtitles = False
                captions = CAPTIONS.get(job_id)
                await guard()
                await pipeline.process_job(
                    job_id, request.url,
                    captions.page_subtitles if captions else None,
                    captions.page_subtitle_language if captions else None,
                    request.identity.cid if captions and captions.page_subtitles else None,
                    captions.page_subtitle_provenance.model_dump() if captions and captions.page_subtitle_provenance else None,
                    cache_key_override=work_key,
                )
                await guard()
                # Keep the native service lease until result/checkpoint upload
                # finishes; the extension releases it on a terminal job state.
                terminal_state = job.state
                job.state = "running"
                if job.result and job.summary_state == "completed":
                    record = make_record(request.identity, job.result.model_dump(), True, config)
                    save_record(record)
                    queue_record(record, config)
                    if lease:
                        try:
                            job.stage = "正在同步处理结果"
                            await client.submit(record, lease)
                            lease = None
                            discard_queued(record)
                            job.shared_state = "synced"
                            job.stage = "处理完成，已同步"
                        except SharedError as exc:
                            job.sync_error = str(exc)
                    if request.local_only or job.sync_error:
                        job.stage, job.shared_state = "结果完成，等待同步", "pending"
                    return
                return
            except Exception:
                if not interrupted and control.resume_event.is_set() and not control.cancelled and not control.shared_stop_event.is_set():
                    raise
                if control.cancelled:
                    raise asyncio.CancelledError
                job.error = None
                job.stage = "等待重新申请共享任务"
                job.state = "paused" if not control.resume_event.is_set() else "running"
            finally:
                if monitor:
                    monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor
                    monitor = None
                await release()
                control.shared_guard = None
    except asyncio.CancelledError:
        job.state, job.stage = "cancelled", "任务已取消，进度已保留"
    except Exception as exc:
        job.state, job.stage, job.error = "failed", "共享处理失败", str(exc)
    finally:
        if monitor:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        await release()
        await client.close()
        if terminal_state and job.state == "running":
            job.state = terminal_state
        control.shared_guard = None
        control.shared_stop_event = None
        job.needs_subtitles = False
        pipeline.JOB_TASKS.pop(job_id, None)
        CAPTIONS.pop(job_id, None)


async def sync_loop():
    while True:
        config = load_config()
        if config.shared_cache_enabled and config.shared_cache_url:
            with suppress(Exception):
                await flush_outbox(config)
        await asyncio.sleep(30)
