"""Romdex retrieval contract v1.0: wire models, and the in-memory store of running fetches.

The core sends settings with every call and candidates carry everything needed to stage them, so the only state a
plugin keeps is its running jobs. See docs/guide/writing-a-retriever-plugin.md in the Romdex repository.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

CONTRACT_VERSION = "1.0"


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class TargetFile(Model):
    name: str
    size: int
    crc32: str | None = None
    md5: str | None = None
    sha1: str | None = None


class TargetDump(Model):
    name: str
    regions: list[str] = []
    serial: str | None = None
    files: list[TargetFile] = []


class Target(Model):
    system: str
    title: str
    dumps: list[TargetDump] = []
    dump_name: str | None = None


class Candidate(Model):
    candidate_id: str
    name: str
    source: str
    dump_name: str | None = None
    url: str | None = None
    format: str | None = None
    size: int | None = None
    verified: bool | None = None
    score: float | None = None
    notes: str | None = None


class HealthRequest(Model):
    settings: dict[str, Any] = {}


class SearchRequest(Model):
    settings: dict[str, Any] = {}
    target: Target
    limit: int = 10


class ListRequest(Model):
    settings: dict[str, Any] = {}


class FetchRequest(Model):
    settings: dict[str, Any] = {}
    job_id: str
    target: Target | None = None
    candidate: Candidate
    staging_directory: str


def dump(model: BaseModel | list[BaseModel]) -> Any:
    """The wire form: camelCase, nulls left out."""
    if isinstance(model, list):
        return [dump(m) for m in model]
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def error(status: int, message: str, code: str | None = None) -> HTTPException:
    return HTTPException(status, {"error": message, **({"code": code} if code else {})})


class Cancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    state: str = "queued"
    progress: float | None = None
    files: list[dict[str, Any]] | None = None
    error: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    finished_at: float | None = None

    def status(self) -> dict[str, Any]:
        body: dict[str, Any] = {"jobId": self.id, "state": self.state}
        if self.progress is not None:
            body["progress"] = round(self.progress, 3)
        if self.files is not None:
            body["files"] = self.files
        if self.error:
            body["error"] = self.error
        return body

    def check(self) -> None:
        """Raises if the core cancelled the job; long-running work calls this between steps."""
        if self.cancelled.is_set():
            raise Cancelled()


class Jobs:
    """Fetches in flight, run on a small thread pool. Finished jobs are forgotten after an hour."""

    def __init__(self, workers: int, keep_seconds: float = 3600):
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep_seconds

    def start(self, job_id: str, work: Callable[[Job], list[dict[str, Any]]]) -> Job:
        with self._lock:
            self._forget_old()
            if job_id in self._jobs:
                return self._jobs[job_id]
            job = self._jobs[job_id] = Job(job_id)
        self._pool.submit(self._run, job, work)
        return job

    def running(self) -> set[str]:
        """The jobs queued or running."""
        with self._lock:
            return {j.id for j in self._jobs.values() if j.state in ("queued", "running")}

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job, work: Callable[[Job], list[dict[str, Any]]]) -> None:
        try:
            job.check()
            job.state = "running"
            job.files = work(job)
            job.state = "completed"
            job.progress = 1
        except Cancelled:
            job.state = "cancelled"
        except Exception as e:  # anything the work raises fails the job with its message
            job.state = "failed"
            job.error = str(e) or type(e).__name__
        finally:
            job.finished_at = time.monotonic()

    def _forget_old(self) -> None:
        now = time.monotonic()
        for job_id in [j.id for j in self._jobs.values() if j.finished_at and now - j.finished_at > self._keep]:
            del self._jobs[job_id]


def staging_directory(root: str, relative: str) -> Path:
    """The job's directory under the staging root; refuses paths that escape it."""
    base = Path(root).resolve()
    directory = (base / relative).resolve()
    if directory == base or base not in directory.parents:
        raise error(400, "stagingDirectory must be inside the staging volume.", "invalid-staging-directory")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def env(name: str, default: str) -> str:
    return os.environ.get(name) or default
