"""Probe‑Plan‑Teach 超级学习系统 后端入口。

启动：uvicorn main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import threading
import time

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from auth import get_current_user
from config import DATA_ROOT, FRONTEND_DIR, HOST, PORT, SESSION_IDLE_SAVE_SECONDS, ensure_dirs
from core.session_manager import session_manager

ensure_dirs()

app = FastAPI(title="Probe‑Plan‑Teach 超级学习系统", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api import auth_routes
from api import projects as projects_api
from api import session as session_api
from api import admin as admin_api
from api import sync as sync_api

app.include_router(auth_routes.router)
# 业务路由全部要求登录（JWT）；auth 路由自身处理匿名注册/登录
app.include_router(projects_api.router, dependencies=[Depends(get_current_user)])
app.include_router(session_api.router, dependencies=[Depends(get_current_user)])
app.include_router(sync_api.router, dependencies=[Depends(get_current_user)])
app.include_router(admin_api.router)


# 静态前端
@app.get("/")
def index():
    resp = FileResponse(FRONTEND_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


class NoCacheStaticFiles(StaticFiles):
    """静态资源不缓存：确保手机/浏览器每次都拿到最新前端（配合 ?v=N 双保险）。"""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


if FRONTEND_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=FRONTEND_DIR), name="static")


def _idle_sweeper() -> None:
    while True:
        time.sleep(60)
        try:
            n = session_manager.sweep_idle()
            if n:
                print(f"[sweep] 回收闲置会话 {n} 个（已保存快照）")
        except Exception as e:  # 兜底，不让清理线程挂掉
            print(f"[sweep] 异常：{e}")


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=_idle_sweeper, daemon=True).start()
    print(f"[startup] 数据根目录: {DATA_ROOT}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
