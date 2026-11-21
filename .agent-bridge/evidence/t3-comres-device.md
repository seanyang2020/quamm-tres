---
task_id: t3-comres-device
---

# T3 COMRes：串口注册设备（自动建档）+ 资源池三态

## Diff 摘要

| 文件 | 变更 |
|---|---|
| `app/constants.py` | `RegisterChannel.COMRES_TCP = "comres_tcp"` |
| `app/domain/comres.py` | `_create_device_from_port` / `_fill_device_missing` / 冲突检测；`_apply_auto_match` 建档路径；`_is_allocated` 仅 allocated_by；`expire` 保留 bound；`list_resources` → allocated/bound/pool |
| `tests/smoke_comres.py` | ACCEPT-5/6 适配三态；ACCEPT-8 建档/幂等/冲突/无身份 |

提交：`5a3c1fd`（父：`30f80a3`）

## a. 编译/构建

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres.py app/constants.py tests/smoke_comres.py`  
退出码：`0`

## b. 改动文件清单

- `app/constants.py`
- `app/domain/comres.py`
- `tests/smoke_comres.py`

## c. 版本/提交证据

```text
5a3c1fd feat(comres): 串口上报 sn/mac 自动建档，资源视图三态
```

`git diff --stat 30f80a3..HEAD`：3 files, +313/-75

## d. 验证/自测

- `tests/smoke_comres.py` → `ALL_PASS` / exit 0
- `tests/smoke_wirelog.py` → `ALL_PASS` / exit 0
- `POST /api/comres/register` 无 token → 401
