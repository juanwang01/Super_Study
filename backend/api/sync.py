"""跨设备同步 API：manifest / pull / push（服务器权威 + hash 增量 + 冲突副本）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from api.projects import _check_owner
from core import sync_engine

router = APIRouter(prefix="/api/sync", tags=["sync"])


class PullBody(BaseModel):
    paths: list[str] = []


class ChangeItem(BaseModel):
    path: str
    content: str
    base_hash: str = ""
    encoding: str = "utf-8"


class PushBody(BaseModel):
    changes: list[ChangeItem] = []
    deleted: list[str] = []


@router.post("/{project_id}/manifest")
def sync_manifest(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    files = sync_engine.list_sync_files(project_id, user.get("id", ""))
    return {"ok": True, "files": files, "count": len(files)}


@router.post("/{project_id}/pull")
def sync_pull(project_id: str, body: PullBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    files = sync_engine.pull_files(project_id, user.get("id", ""), body.paths)
    return {"ok": True, "files": files}


@router.post("/{project_id}/push")
def sync_push(project_id: str, body: PushBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    changes = [c.model_dump() for c in body.changes]
    result = sync_engine.push_files(project_id, user.get("id", ""), changes, body.deleted)
    return result
