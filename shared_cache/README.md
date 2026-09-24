# Private cross-machine cache / 私有跨机器缓存

This optional service stores subtitles, translations, summaries and completed-extraction checkpoints. Processing stays on your computers. It does not need FFmpeg, Whisper, GPU libraries, browser cookies or your LLM API key.

此服务只保存字幕原文、译文、摘要和已完成提取的处理断点。视频下载、Whisper 识别和模型请求仍在电脑上运行；服务端无需安装 FFmpeg 或语音模型。

## Deploy / 部署

On a Linux server or NAS with Docker Compose, from the repository root:

```sh
# Generate a private token; save it in your password manager.
export SHARED_CACHE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f shared_cache/compose.yaml up -d --build
docker compose -f shared_cache/compose.yaml ps
```

Keep this token for future Compose commands and configure the same value on your computers. Do not commit it. The example binds only `127.0.0.1:18766`. Put an HTTPS reverse proxy in front of it for access from other computers. A single server instance uses a persistent SQLite volume; do not scale replicas or put its database on a network filesystem.

保存生成的令牌，后续 Compose 命令仍需要它。所有电脑填写同一个令牌。示例仅监听服务器本机 `127.0.0.1:18766`，请通过 HTTPS 反向代理供其他电脑访问。SQLite 数据存储在持久卷中，只运行一个实例，不要把数据库放在网络文件系统上。

Example Nginx location inside your existing HTTPS virtual host:

```nginx
location / {
    client_max_body_size 32m;
    proxy_pass http://127.0.0.1:18766;
    proxy_set_header Host $host;
    proxy_read_timeout 30s;
}
```

Alternatively, in an isolated Python 3.12 environment, install `shared_cache/requirements.txt`, set `SHARED_CACHE_TOKEN` and `SHARED_CACHE_DB`, then run from the repository root:

```sh
python -m uvicorn shared_cache.server:app --host 127.0.0.1 --port 18766 --no-access-log
```

The server refuses cache access when no token is configured. `/health` exposes only health/protocol information; all `/v1` routes require `Authorization: Bearer <token>`.

## Configure computers / 配置电脑

1. Reload the Chrome extension and restart its local service after updating the code.
2. Open **Settings → More settings → Shared cache**. Enable sharing; enter the HTTPS service URL and token; save and test the connection.
3. On the computer with existing results, choose **Import existing cache**. Import accepts complete v8 files and checks Bilibili resource identity through its official API. Damaged, older or incomplete files are skipped with a reason. Keep the settings page open while importing.
4. Process the same video on another computer. The panel should report that the shared cache was loaded, without audio download or model calls.

1. 更新代码后重载扩展、重启本机服务。
2. 在 **设置 → 更多设置 → 共享缓存** 启用共享，填写服务地址和访问令牌，保存后测试连接。
3. 在存有旧结果的电脑点击 **导入已有缓存**。仅接受完整 v8 文件，通过 B 站官方接口确认视频身份；旧版本、断点、损坏文件及无法确认身份的文件会跳过并说明原因。导入期间保持设置页打开。
4. 在另一台电脑处理同一视频，面板应显示 **已从共享缓存加载**，不下载音频、不调用模型。

## Behavior / 行为

- Local results are read first. Bilibili keys use BVID + CID, so different parts stay isolated while AV/BV/watch-later links and subtitle/Whisper sources can share one result. Official resource identity is resolved before requesting page subtitle tracks.
- A 90-second server lease, renewed every 20 seconds, chooses one processing computer. Other computers wait; expired leases allow takeover. Fenced writes reject old owners. Network partitions can still duplicate in-flight requests; this is not an exactly-once guarantee for external model calls.
- Completed source extraction and translated batches are shared. Summary streaming checkpoints are throttled to about five seconds. Unfinished Whisper recognition remains local, and audio is never synchronized.
- When offline, existing local results work. Otherwise the job waits, with an explicit **Process locally** override that may incur duplicate work. Finished results are saved locally before upload. A persistent outbox retries every 30 seconds while the local service is running, including subsequent service starts. Changing the service URL or token does not send an old outbox to a different destination.
- Model and prompt changes do not invalidate results. **Regenerate shared result** requests a new coordinated run and replaces the server result only on success. Other machines keep their existing local copies until local subtitle cache cleanup; clearing local cache does not delete server results.
- Shared records and the outbox live under `%LOCALAPPDATA%\YouTubeBilingualAssistant\shared-cache`; legacy v8 and local processing checkpoints remain in `cache`. Turning sharing off restores the legacy local processing path. Neither directory has automatic time-based expiry.
- There is a 32 MiB limit per upload request, including its JSON envelope. Oversized results remain local and show a synchronization error.

- 优先使用本机结果。B 站以 BVID + CID 为键，同一分 P 在 AV/BV/稍后再看入口以及站点字幕/Whisper 来源之间可复用，不同分 P 不混用。
- 任务占用有效期 90 秒，每 20 秒续期；其他电脑等待，过期可接管。旧执行者不能覆盖新结果，但故障时已发出的模型请求可能重复。
- 跨机器同步完整原文、已翻译批次及摘要进度；未完成的 Whisper 识别断点仅留本机，不同步音频。
- 断线时无本机结果则等待，可手动选择 **仅本机处理**。完成结果先落本机，再上传；待同步队列在本机服务运行期间每 30 秒重试，下次启动也会继续。
- 不同模型/提示词默认复用。**重新生成共享结果** 成功后才替换服务端结果，失败保留旧结果。其他电脑已保存的本机副本优先使用，需要清理本机字幕缓存后才能取回更新版本。
- 清理本机字幕缓存不清理服务端；关闭共享恢复原来的本机流程。首版不提供远程批量清空。

## Backup, restore, upgrade / 备份、恢复、升级

Stop the container before copying database files. This checkpoints/closes SQLite and avoids inconsistent WAL copies. Back up the entire volume, including any `-wal` and `-shm` files. Use your NAS/container volume backup facility or:

```sh
docker compose -f shared_cache/compose.yaml stop cache
docker compose -f shared_cache/compose.yaml run --rm --no-deps --user 0 \
  -v "$PWD/backups:/backup" --entrypoint sh cache \
  -c 'tar -czf /backup/shared-cache.tar.gz -C /data .'
docker compose -f shared_cache/compose.yaml start cache
```

For restore, stop the service, restore that archive into the same `/data` volume, preserve UID/GID `10001`, then start it. Back up before `up -d --build` upgrades; retain the previous image for rollback. Do not use `down -v` unless deliberately deleting all shared data. Monitor container health, free disk space, connection-test results and client pending-upload counts. No external server is deployed by installing the extension.

备份前停止容器，备份整个数据卷（包含可能存在的 WAL 文件）。恢复时保持服务停止，将备份还原到 `/data`，保证 UID/GID 为 `10001`，再启动。升级前备份并保留旧镜像。不要执行 `down -v`，它会删除共享数据卷。可通过容器健康检查、磁盘空间、客户端连接测试及待同步数量监控运行情况。安装扩展不会自动部署远程服务器。
