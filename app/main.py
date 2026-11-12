"""应用入口：一个 app 挂两个路由组（Web API + MCP），同一进程。

本地启动：python -m uvicorn app.main:app --port 9100
  - Web 控制台  http://localhost:9100/
  - MCP 端点    http://localhost:9100/mcp/  （streamable HTTP）
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import router as api_router
from app.api.wirelog_router import router as wirelog_router
from app.config import settings
from app.db import SessionLocal, init_db
from app.domain.probe import setup_prober_from_settings
from app.mcp.server import build_mcp_app
from app.migrations import migrate
from app.seed import seed_admin, seed_contacts, seed_product_types

mcp_app = build_mcp_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 进程启动时按 settings.probe_backend 选定探测后端（默认 mock）。
    # 注册（MCP）与巡检（worker）共用同一实现。
    # v0.28：探测后端（mock/real）以运行时设置为准，重启后不会退回默认 mock
    with SessionLocal() as _probe_session:
        setup_prober_from_settings(_probe_session)
    with SessionLocal() as session:
        migrate(session)          # 补 device/credential 新列 + 修正管理员标志（老库不丢数据）
        seed_product_types(session)
        seed_admin(session)       # 确保 admin@corp.com 存在
        seed_contacts(session)    # /tmp/contacts_seed.json 17 人（幂等）
    # 挂载 FastMCP 的 lifespan（初始化 streamable HTTP 会话管理）
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="Quamm TRES", lifespan=lifespan)
app.include_router(api_router)
app.include_router(wirelog_router, prefix="/api")
app.mount("/mcp", mcp_app)

# 静态看板：优先 web/dist（Vue 构建产物），否则退化为内联看板
_WEB_DIST = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
_FALLBACK_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Quamm TRES 看板</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#222}
.card{display:inline-block;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:.5rem;min-width:160px}
.num{font-size:2rem;font-weight:600}</style></head><body>
<h1>Quamm TRES 看板</h1><p id="hint">加载中…</p><div id="cards"></div>
<script>
fetch('/api/dashboard').then(r=>r.json()).then(d=>{
  const cards=document.getElementById('cards');
  const box=(t,n)=>`<div class="card"><div>${t}</div><div class="num">${n}</div></div>`;
  cards.innerHTML=box('设备总数',d.total_devices)+box('活跃租约',d.active_leases)+box('排队中',d.queued);
  const p=document.getElementById('hint');
  p.textContent= d.last_patrol ? ('上次巡检：'+d.last_patrol.ended_at) : '尚未巡检';
}).catch(e=>{document.getElementById('hint').textContent='加载失败：'+e;});
</script></body></html>"""

if os.path.isdir(_WEB_DIST) and os.path.isfile(os.path.join(_WEB_DIST, "index.html")):
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    def _fallback_index():
        return _FALLBACK_HTML
