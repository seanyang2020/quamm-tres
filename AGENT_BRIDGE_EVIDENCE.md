---
task_id: tres-interactive-shell-fallback-20261124
---

# tres-interactive-shell-fallback-20261124：交互式 adbd 变体探测回退

## a. 编译/构建

- 命令：`.venv/bin/python -m py_compile app/domain/prober_real.py`
- 退出码：`0`
- 命令：`timeout 60 .venv/bin/python -m pytest tests/test_prober_l3.py tests/test_prober_interactive_fallback.py -q`
- 退出码：`0`；结果：`11 passed`
- 命令：`timeout 90 .venv/bin/python -m pytest tests/test_prober_l3.py tests/test_prober_interactive_fallback.py tests/test_pending_probe.py -q`
- 退出码：`0`；结果：`17 passed`
- 全量 pytest（参考）：`201 passed, 17 failed` — 失败均在 flash/apply/registration/mcp_http/queue，与本次 prober 改动无关（改动前同域已红）

## b. 改动文件清单

- `app/domain/prober_real.py` — 交互式 shell 回退（标记切分、超时/杀进程、字段映射）
- `tests/fake_adb_interactive.py` — fake adb（interactive / normal 两种 adbd）
- `tests/test_prober_interactive_fallback.py` — 新单测
- `tests/test_prober_l3.py` — tcp 预检 mock + shell_dead 时 mock 交互回退失败
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md` — 本任务证据

## c. 版本/提交证据

- 分支：`feat/comres`
- 主提交：`8fd9078 feat(0.50.0): real 探测器兼容忽略 shell 参数的交互式 adbd 变体`
- `git rev-parse`（主提交）：`8fd9078127a9100bc9c35d42076aa8a438ebace6`
- `git show --stat 8fd9078`：6 files, +622/-125
- Gerrit change：不适用
- 说明：本文件若另有「证据 hash 回填」小提交，以 `git log -1` 为准；功能改动在 `8fd9078`

## d. 验证/自测

### d1. 真机 172.16.60.206（变体 adbd）

```
206 fallback OFF: level= L0 shell_ok= False product= None mac= None
206 fallback ON:  level= L3 shell_ok= True product= nxf7e mac= dc:0d:30:07:01:00 src= wlan0 board= arm,molchip,xc01 arm,molchip
```

退出码：`0`

验收：health 非 L0（L3）；product_name=nxf7e；MAC 有值；mac_source=wlan0（与既有枚举一致）

### d2. 对照正常设备 172.16.60.198（fallback on vs off）

```
DIFFS= NONE
OFF/ON 均为 L3 framex mac=a0:ac:78:a0:d7:50 mac_source=wlan0 firmware=V1.00.00.944 ...
```

### d3. 单测

- 变体 A（interactive）→ L3 + nxf7e + wlan0 MAC
- 变体 B（normal）→ L3 + eth0 优先
- 关闭回退 + interactive → L0（与改动前一致）
- shell dead + 交互失败 → L0

### 验收结论

| 标准 | 结论 |
|---|---|
| 206 非 L0 + nxf7e + MAC/wlan | ✅ L3 / nxf7e / dc:0d:30:07:01:00 / wlan0 |
| 正常设备 on/off 字段一致 | ✅ 198 DIFFS=NONE |
| 单测 A/B + pytest 无新增失败 | ✅ 11/11 prober 相关通过 |
| 关闭回退 = 改动前（206→L0） | ✅ |
