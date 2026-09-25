import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from service.app import pipeline, shared_client, shared_jobs, shared_routes
from service.app.models import Segment, ServiceConfig, VideoRequest, PageSubtitleIdentity
from service.app.shared_schema import CacheRecord, ResourceIdentity
from shared_cache.server import create_app


def identity(cid=123):
    return ResourceIdentity(platform="bilibili", video_id="BV1test123", cid=cid)


def record(complete=True, cid=123):
    return CacheRecord(identity=identity(cid), title="Test", source="whisper", source_language="en",
        segments=[Segment(start=0, end=1, en="hello", zh="你好" if complete else "")],
        summary="摘要" if complete else "", summary_state="completed" if complete else "idle",
        subtitle_timing_version=2, complete=complete)


@pytest.fixture
def server(tmp_path):
    now = [1000.0]
    app = create_app(tmp_path / "server.sqlite3", "private", clock=lambda: now[0])
    client = TestClient(app, headers={"Authorization": "Bearer private"})
    return app, client, now


def claim(client, owner="a", regenerate=False, key=None):
    return client.post(f"/v1/cache/{key or identity().key}/claim", json={"owner": owner, "regenerate": regenerate}).json()


def lease(value):
    return {k: value[k] for k in ("generation", "token")}


def submit(client, value, content=None):
    return client.put(f"/v1/cache/{identity().key}", json={**lease(value), "record": (content or record()).model_dump()})


def test_atomic_concurrent_claim_and_fenced_takeover(server):
    _, client, now = server
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: claim(client, str(n)), range(4)))
    assert sum(item["state"] == "acquired" for item in results) == 1
    first = next(item for item in results if item["state"] == "acquired")
    assert submit(client, first, record(False)).status_code == 200
    now[0] += 91
    second = claim(client, "next")
    assert second["generation"] > first["generation"]
    assert second["checkpoint"]["segments"][0]["en"] == "hello"
    assert submit(client, first).status_code == 409
    assert submit(client, second).status_code == 200
    assert claim(client)["state"] == "complete"


def test_renew_release_and_regeneration_preserve_previous_result(server):
    _, client, now = server
    first = claim(client)
    now[0] += 80
    assert client.post(f"/v1/cache/{identity().key}/renew", json=lease(first)).status_code == 200
    now[0] += 20
    assert claim(client)["state"] == "busy"
    assert submit(client, first).status_code == 200
    second = claim(client, regenerate=True)
    assert claim(client)["record"]["summary"] == "摘要"
    assert submit(client, second, record(False)).status_code == 200
    assert client.post(f"/v1/cache/{identity().key}/release", json=lease(second)).status_code == 200
    assert claim(client)["record"]["summary"] == "摘要"
    assert submit(client, second).status_code == 409


def test_auth_identity_corruption_and_part_isolation(server):
    app, client, _ = server
    assert TestClient(app).get("/v1/status").status_code == 401
    assert TestClient(app).post(f"/v1/cache/{identity().key}/claim", json={"owner": "a"}).status_code == 401
    first = claim(client)
    assert submit(client, first, record(cid=999)).status_code == 422
    assert claim(client, key=identity(999).key)["state"] == "acquired"
    data = record().model_dump()
    data["segments"][0]["end"] = 0
    assert client.put(f"/v1/cache/{identity().key}", json={**lease(first), "record": data}).status_code == 422
    data = record().model_dump()
    data["api_key"] = "never-accepted"
    assert client.put(f"/v1/cache/{identity().key}", json={**lease(first), "record": data}).status_code == 422


def test_payload_limit(server, monkeypatch):
    import shared_cache.server as module
    monkeypatch.setattr(module, "MAX_BYTES", 32)
    _, client, _ = server
    response = client.post(f"/v1/cache/{identity().key}/claim", content=b"x" * 33)
    assert response.status_code == 413


@pytest.fixture
def machines(tmp_path, monkeypatch, server):
    app, _, _ = server
    config = ServiceConfig(shared_cache_enabled=True, shared_cache_url="http://cache/", shared_cache_token="private")
    cache = tmp_path / "legacy"
    work = tmp_path / "work"
    cache.mkdir()
    work.mkdir()
    monkeypatch.setattr(pipeline, "CACHE_DIR", cache)
    monkeypatch.setattr(pipeline, "WORK_DIR", work)
    monkeypatch.setattr(pipeline, "load_config", lambda: config)
    monkeypatch.setattr(shared_jobs, "load_config", lambda: config)
    monkeypatch.setattr(shared_routes, "load_config", lambda: config)
    client_type = shared_client.SharedClient
    factory = lambda cfg: client_type(cfg, transport=httpx.ASGITransport(app=app))
    monkeypatch.setattr(shared_jobs, "SharedClient", factory)
    monkeypatch.setattr(shared_routes, "SharedClient", factory)
    monkeypatch.setattr(shared_client, "SharedClient", factory)
    def select(name):
        directory = tmp_path / name
        (directory / "cache").mkdir(parents=True, exist_ok=True)
        (directory / "work").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pipeline, "CACHE_DIR", directory / "cache")
        monkeypatch.setattr(pipeline, "WORK_DIR", directory / "work")
        monkeypatch.setattr(shared_client, "SHARED_DIR", directory)
        monkeypatch.setattr(shared_routes, "SHARED_DIR", directory)
        return directory
    select("a")
    return config, select


async def wait_job(job):
    task = pipeline.JOB_TASKS.get(job.id)
    if task:
        await asyncio.wait_for(task, 5)
    assert job.state == "completed", job.error


def test_machine_a_processes_b_reuses_without_subtitle_download_or_llm(machines, monkeypatch):
    config, select = machines
    calls = []
    class FakeLlm:
        def __init__(self, config):
            calls.append("init")
        async def translate(self, segments, progress, control=None):
            calls.append("translate")
            for segment in segments:
                segment.zh = "你好"
            progress(1, 1)
        async def summarize(self, title, segments, on_stream, resume_from="", control=None):
            calls.append("summary")
            return "摘要", []
        async def close(self):
            pass
    monkeypatch.setattr(pipeline, "LlmClient", FakeLlm)
    monkeypatch.setattr(pipeline, "_download", lambda *a: pytest.fail("must not download"))
    monkeypatch.setattr(pipeline, "_transcribe", lambda *a: pytest.fail("must not transcribe"))
    async def scenario():
        request = shared_jobs.SharedJobRequest(url="https://www.bilibili.com/video/BV1test123?p=1", identity=identity())
        a = shared_jobs.create_shared_job(request)
        for _ in range(100):
            if a.needs_subtitles:
                break
            await asyncio.sleep(0.01)
        assert a.needs_subtitles
        shared_routes.subtitles(a.id, VideoRequest(url=request.url,
            page_subtitle_identity=PageSubtitleIdentity(bvid=identity().video_id, cid=123),
            page_subtitle_status="found", page_subtitle_language="en",
            page_subtitles=[Segment(start=0, end=1, en="hello")]))
        await wait_job(a)
        assert a.shared_state == "synced"
        select("b")
        before = list(calls)
        b = shared_jobs.create_shared_job(request)
        await wait_job(b)
        assert b.cache_origin == "共享"
        assert b.result.segments[0].zh == "你好"
        assert calls == before
        c = shared_jobs.create_shared_job(request)
        await wait_job(c)
        assert c.cache_origin == "本机"
    asyncio.run(scenario())


def test_translated_checkpoint_only_retries_summary(machines, server, monkeypatch):
    _, client, _ = server
    first = claim(client)
    checkpoint = record()
    checkpoint.complete = False
    checkpoint.summary_state = "failed"
    checkpoint.summary = ""
    assert submit(client, first, checkpoint).status_code == 200
    client.post(f"/v1/cache/{identity().key}/release", json=lease(first))
    calls = []
    from service.app.llm import LlmClient
    class FakeLlm(LlmClient):
        async def _request(self, *args):
            pytest.fail("translated cues must not call model")
        async def summarize(self, *args, **kwargs):
            calls.append("summary")
            return "恢复的摘要", []
    monkeypatch.setattr(pipeline, "LlmClient", FakeLlm)
    monkeypatch.setattr(pipeline, "_download", lambda *args: pytest.fail("must not download"))
    async def scenario():
        job = shared_jobs.create_shared_job(shared_jobs.SharedJobRequest(url=identity().url, identity=identity()))
        await wait_job(job)
        assert not job.needs_subtitles
        assert job.result.summary == "恢复的摘要"
        assert calls == ["summary"]
    asyncio.run(scenario())


def test_import_complete_v8_deduplicates_and_skips_unverified(machines, monkeypatch, server):
    legacy = record().result()
    (pipeline.CACHE_DIR / "bilibili_BV1test123_p1_cid123.v8.json").write_text(legacy.model_dump_json(), encoding="utf-8")
    (pipeline.CACHE_DIR / "old.v7.json").write_text(legacy.model_dump_json(), encoding="utf-8")
    (pipeline.CACHE_DIR / "broken.v8.json").write_text("{}", encoding="utf-8")
    async def resolve(url):
        return identity()
    monkeypatch.setattr(shared_routes, "resolve_legacy", resolve)
    monkeypatch.setattr(shared_routes, "IMPORT_CANCEL", False)
    asyncio.run(shared_routes.import_legacy())
    assert shared_routes.IMPORT["uploaded"] == 1
    assert shared_routes.IMPORT["skipped"] == 2
    asyncio.run(shared_routes.import_legacy())
    assert shared_routes.IMPORT["existing"] == 1


def test_outbox_does_not_overwrite_completed_result_or_cross_accounts(machines, server):
    config, _ = machines
    shared_client.queue_record(record(), config)
    async def scenario():
        wrong = config.model_copy(update={"shared_cache_token": "other"})
        await shared_client.flush_outbox(wrong)
        assert list((shared_client.SHARED_DIR / "outbox").glob("*.json"))
        await shared_client.flush_outbox(config)
        assert not list((shared_client.SHARED_DIR / "outbox").glob("*.json"))
        different = record()
        different.summary = "不应覆盖"
        shared_client.queue_record(different, config)
        await shared_client.flush_outbox(config)
    asyncio.run(scenario())
    assert claim(server[1])["record"]["summary"] == "摘要"


def test_cache_checksum_rejects_changed_content():
    original = record()
    data = original.model_dump()
    data["summary"] = "changed"
    with pytest.raises(shared_client.SharedError):
        shared_client.checked_record(data, identity(), original.checksum)


def test_legacy_bilibili_whisper_cache_is_preserved_but_not_reused(machines):
    _, _ = machines
    stale = record().model_copy(update={"subtitle_timing_version": 0})
    shared_client.save_record(stale)

    assert shared_client.any_local_record(identity()) == stale
    assert shared_client.local_record(identity()) is None
    shared_client.preserve_stale_record(stale)

    original = shared_client.SHARED_DIR / "results" / f"{identity().key}.json"
    preserved = shared_client.SHARED_DIR / "results" / f"{identity().key}.timing-v0.json"
    assert CacheRecord.model_validate_json(original.read_text(encoding="utf-8")).subtitle_timing_version == 0
    assert CacheRecord.model_validate_json(preserved.read_text(encoding="utf-8")).subtitle_timing_version == 0


def test_legacy_v8_whisper_cache_is_not_promoted(machines):
    _, _ = machines
    stale_result = record().result().model_copy(update={"subtitle_timing_version": 0})
    key = pipeline.cache_key_from_url(identity().url)
    path = pipeline.CACHE_DIR / f"{key}.v8.json"
    path.write_text(stale_result.model_dump_json(), encoding="utf-8")
    partial = pipeline.CACHE_DIR / "new.partial.v8.json"

    promoted = shared_jobs.promote_legacy(
        shared_jobs.SharedJobRequest(url=identity().url, identity=identity()),
        ServiceConfig(), partial,
    )

    assert promoted is None
    assert path.exists()


def test_stale_remote_whisper_result_is_retranscribed_and_versioned(machines, server, monkeypatch):
    config, _ = machines
    _, remote, _ = server
    stale = record().model_copy(update={"subtitle_timing_version": 1})
    owner = claim(remote)
    assert submit(remote, owner, stale).status_code == 200
    remote.post(f"/v1/cache/{identity().key}/release", json=lease(owner))

    class FakeLlm:
        def __init__(self, config):
            pass
        async def translate(self, segments, progress, control=None):
            segments[0].zh = "你好"
            progress(1, 1)
        async def summarize(self, title, segments, on_stream, resume_from="", control=None):
            return "摘要", []
        async def close(self):
            pass

    def fake_transcribe(*args):
        args[-1](600, 550)
        return [Segment(start=10, end=12, en="hello")], "en"

    monkeypatch.setattr(pipeline, "LlmClient", FakeLlm)
    monkeypatch.setattr(pipeline, "_download", lambda url, directory, *args: (
        {"title": "Reprocessed", "duration": 660}, [], directory / "audio.wav",
    ))
    monkeypatch.setattr(pipeline, "_transcribe", fake_transcribe)

    async def scenario():
        request_url = f"{identity().url}?p=1"
        request = shared_jobs.SharedJobRequest(url=request_url, identity=identity())
        job = shared_jobs.create_shared_job(request)
        for _ in range(100):
            if job.needs_subtitles:
                break
            await asyncio.sleep(0.01)
        assert job.needs_subtitles
        shared_routes.subtitles(job.id, VideoRequest(
            url=request_url,
            page_subtitle_identity=PageSubtitleIdentity(bvid=identity().video_id, cid=123, duration=604),
            page_subtitle_status="no_tracks",
            playback_duration=606,
        ))
        await wait_job(job)
        assert job.result.source == "whisper"
        assert job.result.audio_duration == 600
        assert job.result.subtitle_timing_version == 2
        assert round(job.result.segments[0].start, 6) == 10.1
        assert round(job.result.segments[0].end, 6) == 12.12
        cached = remote.get(f"/v1/cache/{identity().key}").json()["record"]
        assert cached["subtitle_timing_version"] == 2
        assert cached["audio_duration"] == 600

    asyncio.run(scenario())
    preserved = shared_client.SHARED_DIR / "results" / f"{identity().key}.timing-v1.json"
    assert CacheRecord.model_validate_json(preserved.read_text(encoding="utf-8")).subtitle_timing_version == 1


def test_offline_wait_cancel_and_local_hit(machines, monkeypatch):
    class Offline:
        def __init__(self, config):
            pass
        async def claim(self, *a):
            raise shared_client.SharedError("offline")
        async def close(self):
            pass
    monkeypatch.setattr(shared_jobs, "SharedClient", Offline)
    monkeypatch.setattr(pipeline, "_download", lambda *a: pytest.fail("offline must wait"))
    async def scenario():
        request = shared_jobs.SharedJobRequest(url=identity().url, identity=identity())
        job = shared_jobs.create_shared_job(request)
        task = pipeline.JOB_TASKS[job.id]
        await asyncio.sleep(0.02)
        assert job.shared_state == "offline" and not job.needs_subtitles
        pipeline.cancel_job(job.id)
        await asyncio.wait_for(task, 1)
        assert job.state == "cancelled"
        shared_client.save_record(record())
        local = shared_jobs.create_shared_job(request)
        await wait_job(local)
        assert local.cache_origin == "本机"
    asyncio.run(scenario())


def test_pause_releases_owner_and_cancel_waiter_does_not_release_other_owner(machines, server):
    _, client, _ = server
    async def scenario():
        request = shared_jobs.SharedJobRequest(url=identity().url, identity=identity())
        a = shared_jobs.create_shared_job(request)
        for _ in range(100):
            if a.needs_subtitles:
                break
            await asyncio.sleep(0.01)
        b = shared_jobs.create_shared_job(request)
        btask = pipeline.JOB_TASKS[b.id]
        await asyncio.sleep(0.05)
        assert b.shared_state == "waiting"
        pipeline.cancel_job(b.id)
        await asyncio.wait_for(btask, 1)
        assert claim(client, "outsider")["state"] == "busy"
        pipeline.pause_job(a.id)
        await asyncio.sleep(0.7)
        assert a.state == "paused"
        outsider = claim(client, "outsider")
        assert outsider["state"] == "acquired"
        pipeline.resume_job(a.id)
        await asyncio.sleep(0.05)
        assert a.shared_state == "waiting"
        atask = pipeline.JOB_TASKS[a.id]
        pipeline.cancel_job(a.id)
        await asyncio.wait_for(atask, 1)
        assert submit(client, outsider).status_code == 200
    asyncio.run(scenario())


def test_existing_local_v8_is_promoted_without_processing(machines, monkeypatch):
    legacy = record().result()
    path = pipeline.CACHE_DIR / "bilibili_BV1test123_p1.v8.json"
    path.write_text(legacy.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_download", lambda *a: pytest.fail("legacy cache must be reused"))
    async def scenario():
        job = shared_jobs.create_shared_job(shared_jobs.SharedJobRequest(url=identity().url, identity=identity()))
        await wait_job(job)
        assert job.cache_origin == "本机"
        assert path.exists()
        assert shared_client.local_record(identity()).summary == "摘要"
    asyncio.run(scenario())


def test_local_override_queues_without_claiming(machines, monkeypatch):
    config, _ = machines
    partial = pipeline.CACHE_DIR / f"shared_v2_{identity().key[:32]}.partial.v8.json"
    partial.write_text(json.dumps(record().checkpoint(), ensure_ascii=False), encoding="utf-8")
    from service.app.llm import LlmClient
    class NoRequests(LlmClient):
        async def _request(self, *args):
            pytest.fail("all text is already complete")
    monkeypatch.setattr(pipeline, "LlmClient", NoRequests)
    class NoClaim:
        def __init__(self, config):
            pass
        async def claim(self, *a):
            pytest.fail("explicit local override must not claim")
        async def close(self):
            pass
    monkeypatch.setattr(shared_jobs, "SharedClient", NoClaim)
    async def scenario():
        job = shared_jobs.create_shared_job(shared_jobs.SharedJobRequest(url=identity().url, identity=identity(), local_only=True))
        await wait_job(job)
        assert job.shared_state == "pending"
        assert list((shared_client.SHARED_DIR / "outbox").glob("*.json"))
    asyncio.run(scenario())


def test_local_api_routes_and_shared_secret_redaction(machines, server, monkeypatch):
    from service.app import main
    config, _ = machines
    config.base_url = "https://model.invalid/v1"
    config.api_key = "model-private"
    monkeypatch.setattr(main, "load_config", lambda: config)
    stored = []
    monkeypatch.setattr(main, "save_config", stored.append)
    public = main.get_config().model_dump()
    assert public["shared_cache_token_configured"]
    assert "private" not in json.dumps(public)
    main.put_config(config.model_copy(update={"shared_cache_token": "", "api_key": ""}))
    assert stored[0].shared_cache_token == "private"
    assert stored[0].api_key == "model-private"
    assert "private" not in main.put_whisper_model(main.WhisperModelSelection(whisper_model="small")).model_dump_json()
    first = claim(server[1])
    submit(server[1], first)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local") as local:
            data = {"url": identity().url, "identity": identity().model_dump()}
            assert (await local.post("/shared/preflight", json=data)).json()["state"] == "complete"
            response = await local.post("/shared/jobs", json=data)
            assert response.status_code == 200
            job = pipeline.JOBS[response.json()["id"]]
            await wait_job(job)
            assert job.cache_origin == "共享"
            response = await local.post("/shared/jobs/unknown/subtitles", json={"url": identity().url})
            assert response.status_code == 409
    asyncio.run(scenario())


def test_job_stays_active_until_upload_finishes(machines, monkeypatch):
    partial = pipeline.CACHE_DIR / f"shared_v2_{identity().key[:32]}.partial.v8.json"
    partial.write_text(json.dumps(record().checkpoint()), encoding="utf-8")
    factory = shared_jobs.SharedClient
    async def scenario():
        uploading, finish_upload = asyncio.Event(), asyncio.Event()
        class DelayedClient:
            def __init__(self, config):
                self.inner = factory(config)
            def __getattr__(self, name):
                return getattr(self.inner, name)
            async def submit(self, value, lease_value):
                if value.complete:
                    uploading.set()
                    await finish_upload.wait()
                return await self.inner.submit(value, lease_value)
        monkeypatch.setattr(shared_jobs, "SharedClient", DelayedClient)
        job = shared_jobs.create_shared_job(shared_jobs.SharedJobRequest(url=identity().url, identity=identity()))
        await asyncio.wait_for(uploading.wait(), 2)
        assert job.state == "running"
        assert shared_client.local_record(identity()).complete
        finish_upload.set()
        await wait_job(job)
        assert job.shared_state == "synced"
    asyncio.run(scenario())


def test_server_restart_retains_result_and_generation(tmp_path):
    database = tmp_path / "persistent.sqlite3"
    client = TestClient(create_app(database, "private"), headers={"Authorization": "Bearer private"})
    first = claim(client)
    assert submit(client, first).status_code == 200
    restarted = TestClient(create_app(database, "private"), headers={"Authorization": "Bearer private"})
    assert claim(restarted)["record"]["summary"] == "摘要"
    assert claim(restarted, regenerate=True)["generation"] > first["generation"]


def test_lost_lease_stops_new_audio_download(monkeypatch, tmp_path):
    import threading
    stopped = threading.Event()
    class MetadataOnly:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            stopped.set()
            return {}
        def download(self, *args):
            pytest.fail("must not start audio download after losing lease")
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", MetadataOnly)
    with pytest.raises(pipeline.DownloadCancelled):
        pipeline._download(identity().url, tmp_path, stopped)
