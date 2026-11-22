---
task_id: t976
---

# t976：客户端下载收进 COMRes「设置」子页

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`✓ built in 3.91s`；产物 `web/dist/index.html`、`web/dist/assets/index-Df6LOkvU.js`、`web/dist/assets/index-D6OiA4EK.css`

- 命令：`.venv/bin/python -m pytest tests/test_packages.py -q --tb=short`
- 退出码：`0`
- 关键结果：`7 passed`

- 命令：`.venv/bin/python -m pytest tests/ -q --tb=no`
- 退出码：`1`
- 关键结果：`164 passed, 21 failed`；失败集与 t950/t948 基线同批（apply_flow / flash / mcp_http / prober_l3 / queue / registration），**本任务仅前端改动、后端未改，未新增 packages 相关失败**

## b. 改动文件清单

- `web/src/App.vue`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t976.md`
- `.agent-bridge/evidence/t976_PROGRESS.md`
- `.agent-bridge/evidence/t976-shots/comres-settings.png`

## c. 版本/提交证据

```
2a1eb7e / 2a1eb7ebb4ac1b8e35fb7f9f9bd9dbf08154a7d2  ← 功能提交
（交付 HEAD 以 git log -1 --format=%H 为准）
git status --short → 干净（提交后）
git diff --stat 2a1eb7e^..2a1eb7e → 6 files, +254/-169
```

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0 |
| packages 单测 | `pytest tests/test_packages.py -q` | 7 passed；exit 0 |
| 菜单 | CDP/puppeteer 读 `.el-menu-item` | `["设备中心","资源调度","接入申请","凭据","COMRes","设置"]`；无「客户端下载」 |
| 子页签 | `.comres-subtab` | `["节点视图","资源池视图","设置"]` |
| 设置子页 | 点「设置」 | 见自动批准、清理无效资源、客户端下载空态「还没有安装包，管理员可在上方上传」+ admin 上传区 |
| user 权限 | user cookie `POST`/`DELETE /api/packages` | 403「仅管理员可用」 |
| 截图 | `.agent-bridge/evidence/t976-shots/comres-settings.png` | COMRes「设置」子页（空态） |
