"""Portable, secret-free cache records used by clients and the standalone server."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ProcessedVideo, Segment

MAX_BYTES = 32 * 1024 * 1024
BILIBILI_WHISPER_TIMING_VERSION = 1


class ResourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["bilibili", "youtube"]
    video_id: str
    cid: int = 0
    target_language: Literal["zh"] = "zh"
    version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_identity(self):
        pattern = r"BV[0-9A-Za-z]+" if self.platform == "bilibili" else r"[0-9A-Za-z_-]+"
        if not re.fullmatch(pattern, self.video_id) or len(self.video_id) > 64:
            raise ValueError("Invalid video identity")
        if (self.platform == "bilibili" and self.cid <= 0) or (self.platform == "youtube" and self.cid != 0):
            raise ValueError("Invalid resource CID")
        return self

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    @property
    def url(self) -> str:
        if self.platform == "youtube":
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return f"https://www.bilibili.com/video/{self.video_id}"


class CacheRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    identity: ResourceIdentity
    title: str
    duration: float | None = Field(default=None, ge=0)
    audio_duration: float | None = Field(default=None, ge=0)
    subtitle_timing_version: int = Field(default=0, ge=0)
    source: Literal["youtube_subtitles", "bilibili_subtitles", "whisper"]
    source_language: Literal["en", "ja", "ko", "zh"]
    segments: list[Segment] = Field(min_length=1)
    summary_partial: str = ""
    summary_state: Literal["idle", "running", "failed", "completed"] = "idle"
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    complete: bool = False
    # Deliberately only model names / prompt hashes, never endpoints or secrets.
    translation_model: str = ""
    summary_model: str = ""
    whisper_model: str = ""
    translation_prompt_hash: str = ""
    summary_prompt_hash: str = ""

    @model_validator(mode="after")
    def validate_content(self):
        import math
        previous = -1.0
        for segment in self.segments:
            if not math.isfinite(segment.start) or not math.isfinite(segment.end) or segment.end <= segment.start:
                raise ValueError("Invalid cue timing")
            if segment.start < previous or not segment.en.strip():
                raise ValueError("Invalid cue order or empty source")
            previous = segment.start
        if self.source != "whisper" and self.source != f"{self.identity.platform}_subtitles":
            raise ValueError("Source does not match platform")
        if self.subtitle_timing_version and (
            self.source != "whisper" or self.identity.platform != "bilibili"
        ):
            raise ValueError("Subtitle timing version only applies to Bilibili Whisper results")
        if self.complete and (self.summary_state != "completed" or any(not s.zh.strip() for s in self.segments)):
            raise ValueError("Result is not complete")
        return self

    @property
    def timing_current(self) -> bool:
        return not (
            self.identity.platform == "bilibili"
            and self.source == "whisper"
            and self.subtitle_timing_version < BILIBILI_WHISPER_TIMING_VERSION
        )

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def checkpoint(self) -> dict:
        data = self.model_dump(exclude={"identity", "complete"})
        data.update(platform=self.identity.platform, extraction_state="completed")
        return data

    def result(self, url: str | None = None) -> ProcessedVideo:
        return ProcessedVideo(
            video_id=self.identity.video_id, url=url or self.identity.url,
            platform=self.identity.platform, **self.model_dump(include={
                "title", "duration", "audio_duration", "subtitle_timing_version", "source",
                "source_language", "segments", "summary", "key_points",
            }),
        )
