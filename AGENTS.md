# Repository Guidelines

## Project Structure & Module Organization

- `extension/`: Chrome extension JavaScript, HTML, CSS, manifest, and icon. Background messaging, page integration, popup, and settings live here.
- `service/app/`: Python FastAPI service. `main.py` defines endpoints; `pipeline.py` handles captions, transcription, and processing; `llm.py` handles model requests. Configuration, storage, diagnostics, prompts, and models have separate modules.
- `native-host/Program.cs`: C# native messaging bridge that manages the local service.
- `scripts/`: PowerShell installation, startup, model download, update, and packaging tools.
- `tests/`: pytest coverage for APIs, pipelines, storage, diagnostics, and extension behavior.
- `README.md` and `README_zh.md`: English and Chinese user documentation.

## Build, Test, and Development Commands

Run commands from the repository root on Windows with Python 3.11+, Chrome, and FFmpeg installed.

- `.\scripts\install.ps1`: create `.venv` and install `requirements.txt`.
- `.\scripts\install-native-host.ps1 -ExtensionId "<extension-id>"`: build/register the native host after loading `extension/` through Chrome's **Load unpacked** action.
- `.\scripts\start.ps1`: start the service at `http://127.0.0.1:18765`.
- `.\.venv\Scripts\python.exe -m pytest tests`: run the test suite. Append a filename to run focused tests.
- `.\scripts\package.ps1`: produce `dist/youtube-bilingual-assistant.zip`.

Reload the unpacked extension after frontend changes; no JavaScript build step is configured.

## Coding Style & Naming Conventions

Follow surrounding code: four-space indentation for Python and C#, two-space indentation for JavaScript. Use Python `snake_case` functions and variables, `PascalCase` classes, and JavaScript `camelCase` identifiers. Preserve Python type annotations and existing C# brace placement. Keep files UTF-8 for Chinese text. No formatter or linter configuration is checked in; avoid unrelated formatting changes.

## Testing Guidelines

Use pytest files named `test_*.py` and functions named `test_*`. Follow existing `tmp_path`, `monkeypatch`, and FastAPI `TestClient` patterns to isolate configuration, files, and external services. Add regression coverage for changed behavior. No numerical coverage threshold is configured. For player/UI changes, manually check affected YouTube or Bilibili flows and include screenshots.

## Commit & Pull Request Guidelines

Recent commits use short Chinese action descriptions, such as `修复摘要空状态与全屏滚轮冲突`; release messages include a version. Keep commits focused and descriptive. PRs should explain the problem, resulting behavior, validation performed, and linked issues where applicable. Update both READMEs when user workflows change.

## Security & Configuration

Keep API keys, cookies, downloaded models, and runtime caches out of commits. Default settings and task data live under `%LOCALAPPDATA%\YouTubeBilingualAssistant`. Preserve secret redaction in API responses and diagnostics.
