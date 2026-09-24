from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import threading
from contextlib import contextmanager, closing
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from service.app.shared_schema import CacheRecord, MAX_BYTES

LEASE_SECONDS = 90


class Claim(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    regenerate: bool = False


class Lease(BaseModel):
    generation: int
    token: str


class Submission(Lease):
    record: CacheRecord


def create_app(database: str | Path | None = None, token: str | None = None, clock=time.time) -> FastAPI:
    database = Path(database or os.getenv("SHARED_CACHE_DB", str(Path(__file__).parent / "data" / "cache.sqlite3")))
    access_token = token if token is not None else os.getenv("SHARED_CACHE_TOKEN", "")
    app = FastAPI(title="Private video cache", docs_url=None, redoc_url=None)
    initialized = False
    initialization_lock = threading.Lock()

    @contextmanager
    def transaction():
        nonlocal initialized
        with initialization_lock:
            if not initialized:
                database.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(database, timeout=15)) as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("""CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY, result TEXT, checkpoint TEXT,
                        generation INTEGER NOT NULL DEFAULT 0, owner TEXT, token TEXT,
                        expires REAL NOT NULL DEFAULT 0, regenerate INTEGER NOT NULL DEFAULT 0)""")
                    db.commit()
                initialized = True
        with closing(sqlite3.connect(database, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def authorize(authorization: str = Header(default="")):
        if not access_token:
            raise HTTPException(503, "Server access token not configured")
        if not hmac.compare_digest(authorization, f"Bearer {access_token}"):
            raise HTTPException(401, "Invalid access token")

    # Bound streamed bodies too, rather than trusting Content-Length.
    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        from fastapi.responses import JSONResponse
        if request.method in ("POST", "PUT"):
            if not access_token or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {access_token}"):
                return JSONResponse({"detail": "Invalid access token"}, status_code=401)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return JSONResponse({"detail": "Cache payload exceeds 32 MiB"}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    def valid_key(key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise HTTPException(400, "Invalid cache key")

    def lookup(db, key):
        valid_key(key)
        return db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()

    def checked_lease(db, key, lease):
        row = lookup(db, key)
        if not row or row["generation"] != lease.generation or not hmac.compare_digest(row["token"] or "", lease.token) or row["expires"] <= clock():
            raise HTTPException(409, "Lease expired or superseded")
        return row

    def payload(row):
        record = CacheRecord.model_validate_json(row["result"]) if row and row["result"] else None
        checkpoint = CacheRecord.model_validate_json(row["checkpoint"]) if row and row["checkpoint"] else None
        return {"state": "complete" if record else "busy" if row and row["expires"] > clock() else "missing",
                "record": record.model_dump() if record else None,
                "checksum": record.checksum if record else None,
                "checkpoint": checkpoint.model_dump() if checkpoint else None}

    @app.get("/health")
    def health():
        return {"ok": bool(access_token), "protocol": 1}

    @app.get("/v1/status", dependencies=[Depends(authorize)])
    def status():
        return {"ok": True, "protocol": 1}

    @app.get("/v1/cache/{key}", dependencies=[Depends(authorize)])
    def get(key: str):
        with transaction() as db:
            return payload(lookup(db, key))

    @app.post("/v1/cache/{key}/claim", dependencies=[Depends(authorize)])
    def claim(key: str, request: Claim):
        with transaction() as db:
            row = lookup(db, key)
            if row and row["result"] and not request.regenerate:
                return payload(row)
            if row and row["expires"] > clock():
                return {"state": "busy"}
            generation = row["generation"] + 1 if row else 1
            lease_token = secrets.token_urlsafe(32)
            db.execute("""INSERT INTO cache(key,generation,owner,token,expires,regenerate) VALUES(?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,owner=excluded.owner,
                token=excluded.token,expires=excluded.expires,regenerate=excluded.regenerate""",
                (key, generation, request.owner, lease_token, clock() + LEASE_SECONDS, request.regenerate))
            if request.regenerate:
                db.execute("UPDATE cache SET checkpoint=NULL WHERE key=?", (key,))
            return {"state": "acquired", "generation": generation, "token": lease_token,
                    "expires_in": LEASE_SECONDS, "checkpoint": None if request.regenerate or not row or not row["checkpoint"] else json.loads(row["checkpoint"])}

    @app.post("/v1/cache/{key}/renew", dependencies=[Depends(authorize)])
    def renew(key: str, request: Lease):
        with transaction() as db:
            checked_lease(db, key, request)
            db.execute("UPDATE cache SET expires=? WHERE key=?", (clock() + LEASE_SECONDS, key))
        return {"ok": True}

    @app.post("/v1/cache/{key}/release", dependencies=[Depends(authorize)])
    def release(key: str, request: Lease):
        with transaction() as db:
            checked_lease(db, key, request)
            db.execute("UPDATE cache SET expires=0,token=NULL WHERE key=?", (key,))
        return {"ok": True}

    @app.put("/v1/cache/{key}", dependencies=[Depends(authorize)])
    def submit(key: str, request: Submission):
        if request.record.identity.key != key:
            raise HTTPException(422, "Resource identity does not match cache key")
        with transaction() as db:
            checked_lease(db, key, request)
            field = "result" if request.record.complete else "checkpoint"
            db.execute(f"UPDATE cache SET {field}=? WHERE key=?", (request.record.model_dump_json(), key))
            if request.record.complete:
                db.execute("UPDATE cache SET checkpoint=NULL,expires=0,token=NULL WHERE key=?", (key,))
        return {"ok": True, "checksum": request.record.checksum}

    return app


app = create_app()
