---
task_id: t976
---

# t976：TRES 客户端实例下载（wl_package）

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 3.76s`；产物 `web/dist/index.html`、`web/dist/assets/index-hY5jihDP.js`、`web/dist/assets/index-14VofpKn.css`

- 命令：`.venv/bin/python -m pytest tests/test_packages.py -q --tb=short`
- 退出码：`0`
- 关键结果：`7 passed`

## b. 改动文件清单

- `app/models.py`
- `app/config.py`
- `app/migrations.py`
- `app/domain/packages.py`
- `app/api/packages_router.py`
- `app/main.py`
- `web/src/App.vue`
- `tests/test_packages.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t976.md`
- `.agent-bridge/evidence/t976_PROGRESS.md`

## c. 版本/提交证据

```
f78b539 feat(packages): 新增客户端安装包上传/下载分发
f78b539a664bfd771f77d51bcb93f6a82cc235cb
```

- `git log -1 --oneline`：`8a71734 docs(evidence): 回填 t976 客户端下载验收证据`
- `git status --short`：干净
- 功能提交：`f78b539` / `f78b539a664bfd771f77d51bcb93f6a82cc235cb`
- 证据提交：`8a71734` / `8a717348ff2f8b31c95f8cb241530d24ee6dd200`
- Gerrit change：不适用

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 单测 | `pytest tests/test_packages.py -q` | 7 passed；exit 0（上传/列表/下载计数/删除权限/413/扩展名/路径穿越/迁移幂等/channel） |
| 前端构建 | `cd web && npm run build` | exit 0 |
| 真机上传 | admin cookie `POST /api/packages`（`git archive` → `elsb-0.33.0.zip`，version=0.33.0，platform=Windows） | 200；`id=1`，`size_bytes=6072528`，`sha256=bba3c3d2…` |
| 真机列表 | user cookie `GET /api/packages` | `items` 含上述包；`download_count=0` |
| 真机下载 | user cookie `GET /api/packages/1/download` | 200；`Content-Disposition: attachment`；`download_count` → 1 |
| 真机权限 | user `POST` / `DELETE` | 均为 403「仅管理员可用」 |
| 清理 | admin `DELETE /api/packages/1` | 200；再 `GET /api/packages` → `{"items":[]}`；无残留文件 |
