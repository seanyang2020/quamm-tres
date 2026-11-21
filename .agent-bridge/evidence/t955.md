---
task_id: t955
---

# t955：COMRes 节点硬删 500 / 权限提示失真

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`dist/index.html` / `dist/assets/index-*.js` / `dist/assets/index-*.css` 已生成；vite built in 4.29s

- 命令：`.venv/bin/python -m pytest tests/test_wirelog_delete_node.py tests/test_comres_apply.py -q --tb=short`
- 退出码：`0`
- 关键结果：`11 passed`

## b. 改动文件清单

- `app/domain/wirelog.py`
- `app/api/wirelog_router.py`
- `app/domain/comres_apply.py`
- `web/src/App.vue`
- `tests/test_wirelog_delete_node.py`

## c. 版本/提交证据

- 代码提交：`0f5f9e0 fix(comres): 硬删节点级联清空 apply FK，避免 500 并修正权限提示`
- 代码 `%H`：`0f5f9e09c9b31ee317d1ba32286e257bf0d210ee`
- 证据提交（HEAD）：`c6484c5 docs(evidence): 回填 t955 硬删节点修复验收证据` / `c6484c53ddaad7c721b7107cf11619dc715c5141`
- `git status --short`：仅本证据文件后续微调（若有）
- 代码 diff：`5 files changed, 287 insertions(+), 22 deletions(-)`
- Gerrit change：无/不适用

## d. 验证/自测

- pytest：见 a；覆盖 retired 级联删除、非 retired 拒删、非管理员 403、删后同 client_id 重新签发
- 真库：对 `quamm_res.db` 调用 `wirelog.delete_node(..., 'wlf4fd4fc2fed8ceae')`
  - 结果：`deleted=True`，`cascade.applies=1`，`audits=31`
  - 删后节点仅剩 `wl8e9a154c0e5b35f3`、`wleab973ba572f6e5b`
  - apply id=4：`issued_node_id=None`，`token_cipher=None`，`token_prefix` 保留
