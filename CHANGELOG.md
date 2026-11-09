# Changelog

所有值得注意的改动都会记录在此文件。
版本号唯一真源是根目录 `VERSION` 文件；本文件与 git tag 仅作镜像。

## [0.25.0] - 2026-11-09

### 新增：pending 设备端口敲门自动恢复（集成进巡检）
- 背景：部分设备能 ping 通但 5555 无 adb 监听，原流程只能转现场处理。
- 落地：新建 `app/domain/device_recovery.py`，用标准库复现 adbscan 的端口敲门算法，
  行为等价 `adbscan -k -e <eventcode> <ip>`（默认事件码 2223：序列 2223→2232→2322→3222），
  命中设备端守护进程即拉起 adbd，再等待 5555 开放。
- 巡检接入：`run_patrol_round` 对 real 且 ping 通的 pending 设备自动敲门，写审计
  `pending_knock_recover`；adb 起来后重新探测，达 L3 即自动转 available（auto_promote）。
- 防护：每台设备 10 分钟敲门冷却（内存态），避免每轮重复敲门；设备 30s 未起则记录失败。
- 配置：新增 3 个运行时键 `pending_auto_recover_enabled`（bool，默认开）、
  `pending_knock_eventcode`（3–6 位数字，默认 2223）、`pending_knock_timeout`（5–120s，默认 30），
  系统设置页可在线修改；runtime_config 新增字符串类型支持。
- 验证：新增 12 项单测（序列算法/已在监听/恢复成功/超时/冷却/非法事件码），全量
  17 failed/82 passed（17 为旧基线，无新增回归）；实跑巡检确认敲门、审计与自动转正均生效。

### 修复：设备概况刷新后首次加载空白，需切换页面才显示
- 根因：`boot()` 恢复登录态后只加载了看板/版本/健康/机型/编译服务器，**遗漏了
  `loadDevices()`**；带会话直接刷新页面时 `devices` 为空，概况统计与品类卡全显 0，
  直到切换到其他页面再切回设备（触发 `switchPage('devices')`）才加载。
- 修复：在 `boot()` 登录态分支补上 `loadDevices()`，刷新后首次渲染即拉取设备、正常显示。

### 新增：编译服务器第三种认证方式「免密（本机已配置）」
- 背景：若运行 TRES 服务的主机已对编译机配置好免密 SSH（~/.ssh 默认密钥/agent/config），
  无需再生成或粘贴密钥。
- 决策与落地：在 password/key 之外新增 `auth_mode=agent`：
  - 不存储任何凭据（secret_encrypted/public_key 均为空），前端选中后隐藏密钥输入框并给出说明；
  - `remote_ssh.run_remote` 对 agent 模式直接调用系统 `ssh`（不带 -i、不带 sshpass，
    BatchMode 不交互），复用本机已有免密；
  - 编辑切换到该模式时清空已存密钥；列表认证标签显示「免密」。
- 实测：以 172.16.1.202（yangxiongwei）创建 agent 模式服务器，无密钥、探测状态
  available（tcp/认证/工作目录/git/工具链全部通过），首次覆盖真实编译服务器成功路径。

### 优化：新增/编辑机型对话框加宽改两列，字段完整可见
- 背景：机型表单 16 个字段单列纵向堆叠，对话框超出屏幕、底部字段看不全。
- 改造：
  - 对话框加宽（新增 860px、编辑 820px），表单改两列 CSS Grid，长文本字段（SDK/编译/
    备注）跨两列整行，整体高度约减半；
  - label 宽度 120→96、表单项间距收紧，对话框整体高度落在窗口内、无内部纵向滚动；
  - 新增、编辑两个对话框统一布局口径。

### 优化：品类详情设备表紧凑化，去除横向滚动
- 背景：设备表 10 列固定宽度合计约 1370px，窗口稍窄即需手动左右滑动，信息看不全。
- 改造：
  - 文本列由固定 `width` 改为弹性 `min-width`（合计自然宽约 855px），表格按容器宽度
    自适应分配，常规宽屏不再出现横向滚动；
  - 所有文本列统一 `show-overflow-tooltip`，内容超长时单元格内省略号截断，鼠标悬停
    弹出原生 tooltip 显示完整内容；
  - 缩小字号（12px）、收紧单元格内边距，表头同步紧凑；缩短列名（工程配置→工程、
    ADB 地址→ADB），ADB 用等宽字体；
  - 操作列三个按钮改为 `link` 文字按钮（截图/记录/移除），列宽由 250 压到 140。

### 修复：截图预览大图超出弹窗
- 现象：截图弹窗内 `el-image` 只设了 `max-height:55vh` 而无确定高度，内部 img 按自然尺寸
  渲染（竖屏图被宽度放大后高度溢出），超出弹窗、遮挡关闭按钮。
- 修复：给预览容器定高 `height:52vh`、宽 100%，内部 `.el-image__inner` 以
  `object-fit:contain` 等比缩放，图片始终完整落在弹窗内、居中不裁切；点击仍可全屏查看，
  下载原图不受影响。

### 新增：Web 端设备移除（注销）入口
- 此前只有 MCP 提供 `res_device_unregister`，Web 无对应入口。新增管理员路由
  `DELETE /devices/{device_id}`，复用领域 `catalog.device_unregister`（软注销：状态置 retired、
  取消该设备排队、通知等待者并写审计；有活动租约时标 pending_removal，租约结束后自动摘除）。
- 设备表操作列新增「移除」按钮，二次确认后调用并刷新；`list_devices` 默认隐藏 retired 设备
  （显式传 status 仍可查询），看板统计本就不计 retired。
- 权限沿用领域规则：注册人或管理员；错误码映射 NOT_FOUND→404、FORBIDDEN→403、
  HAS_ACTIVE_LEASE→409。

### 优化：登录 token 输入框密码化并支持浏览器保存
- 登录页 token 输入框改为 `type="password"`（带 show-password 可切换明文），并包一层原生
  `<form>`、加 `autocomplete="current-password"`，便于密码管理器识别与回填。
- 因 SPA 异步 fetch 登录不会自动触发浏览器保存提示，登录成功后调用 Credential Management API
  `navigator.credentials.store(new PasswordCredential(...))` 显式请求保存（以登录返回的邮箱为
  账号、token 为密码）；非安全上下文或不支持的浏览器静默跳过，不影响登录。

### 新增：编译服务器（SSH 凭据加密保管 + 自动探测 + SDK 编译升级）
- 新增独立「编译服务器」注册表 `build_server` 表（`BuildServer` 模型）：名称/主机/端口/用户名/
  认证方式(password|key)/加密密钥/公钥/工作根目录/探测状态/探测详情/最近探测时间；机型
  `ProductType` 新增可空 `build_server_id` 引用（inspect 判列后幂等 ALTER，老库不丢数据）。
- 凭据静态加密：新模块 `app/secret_crypto.py` 用 Fernet 加密密码或私钥；主密钥取环境变量
  `QUAMM_MASTER_KEY`，否则首启在 `app/var/master.key` 自动生成（权限 600），主密钥不进数据库；
  接口只回显掩码（末 4 位），绝不返回明文/密文。
- 密钥认证优先：key 模式留空私钥时自动生成 ed25519 密钥对（公钥可在界面查看，放编译机
  authorized_keys）；密码模式用 sshpass，服务端经系统 ssh 集中执行，密钥不下发 agent。
- 新模块 `app/remote_ssh.py`：TCP 可达性 + ssh/sshpass/密钥远程执行（临时私钥 600、用完即删、
  StrictHostKeyChecking=accept-new、超时返回 rc=124）。
- 添加时自动探测（`probe_build_server`）：逐步 tcp→auth→workdir(mkdir -p)→git→工具链，
  落 probe_detail 与 status，全通过才具备 SDK 编译升级能力。
- SDK 编译编排 `app/sdk_build.py`：工作目录 `~/<work_root>/<机型>-<IP尾号>`，依次执行机型的
  sdk_repo_cmd（拉代码）与 build_cmd（编译），返回各步 rc 与日志。
- 接口：REST 新增 /api/build-servers CRUD + /probe、/api/product-types/{code}/build-server、
  /api/sdk-build（均管理员 cookie）；MCP 新增 `res_build_servers`（管理员，list/create/update/
  delete/probe/bind）与 `res_sdk_build_upgrade`（有效凭据可触发，服务端集中执行）。MCP 工具 31→33。
- 前端：设置新增「编译服务器」tab（系统介绍仍为最后一个 tab），机型编辑/新增对话框可选择绑定
  编译服务器并查看状态，编译服务器对话框支持密码/密钥两种方式与探测结果展示。

### 调整：编译服务器改为账号级私有（取消统一编译服务器）
- 归属口径变更：编译服务器不再全局共享、也不再由机型绑定，改为跟随 token 背后的账号（principal，
  以邮箱唯一标识）。`BuildServer` 新增 `principal_id`（外键→principal，唯一），`name` 去掉全局唯一；
  每个账号配置自己的一台、账号自助配置，登录后只显示自己的配置；token 换发或同账号多 token 共享配置。
- 迁移：试用环境 `build_server` 为空表时按新模型 DROP 重建（含 principal_id 唯一、name 非唯一）；
  有数据时幂等 ALTER 加列并建唯一索引。`ProductType.build_server_id` 字段保留但不再使用。
- 领域层 `app/domain/build_servers.py` 重写：按 principal_id 查重（一账号一台，重复抛
  BUILD_SERVER_ALREADY_EXISTS），新增归属裁决（非属主且非管理员返回 FORBIDDEN），list/update/delete/
  probe 均按账号隔离；`app/sdk_build.py` 改为按当前账号的 principal_id 解析自己的编译服务器，
  未配置返回 NO_BUILD_SERVER。
- 接口：REST 编译服务器路由由管理员改为登录态（require_login），移除机型绑定路由
  /product-types/{code}/build-server，FORBIDDEN→403、BUILD_SERVER_ALREADY_EXISTS→409；
  /auth/login 与 /auth/me 返回体增加 principal_id。MCP `res_build_servers` 去掉管理员门禁与 bind
  （任何有效凭据自助操作自己的），`res_sdk_build_upgrade` 按自身 principal_id 解析。
- 前端：「编译服务器」tab 对所有登录用户开放，标题改为「我的编译服务器（账号私有）」，无配置时才显示
  「添加我的编译服务器」按钮，机型编辑/新增对话框移除编译服务器选择器。

### 修复：MCP 注册的真实设备因 source=mock 被误判 offline
- 现象：adb 能连上（如 172.16.60.198），设备页却显示 offline/L0。根因：`Device.source` 列默认
  `mock`，而 MCP 注册路径（`catalog.device_register`）新建设备时未写 source，导致巡检按 mock 后端
  走 HTTP /probe（真机不提供该端点），连续失败达阈值后判 offline；此前的健康修复只改状态不改 source，
  下一轮巡检又被打回。
- 修复：`catalog.device_probe` 在探测结果中记录实际后端 source；`device_register` 新建/更新设备时
  持久化该 source（已有设备重新注册也会校正）。MCP `res_device_probe` 新增可选 source/timeout 参数
  （默认沿用全局后端，保持 mock 测试契约；真实设备可显式 source="real"，Web 手工添加与设备发现路径
  已强制 real）。
- 数据修复：对两台 source 误标为 mock 的真机（dev_997b70adae/172.16.60.198、
  dev_080989bb98/172.16.60.249）重新真实探测确证 L3 后，更正 source=real、状态 available、
  清零连续失败并写审计；全量巡检后稳定保持 available（L3 总数 4→6）。

## [0.24.0] - 2026-11-09

### 新增
- 设置页体验优化：系统设置改为两列紧凑并排布局；「关于 Quamm TRES」并入系统介绍作为第 11 节
（含版本/启动时间/运行时长，经 /api/health 动态填充，离线打开保留占位）；系统介绍新增「在全页面打开」
入口且始终作为设置最后一个 tab
- 添加设备时若自动探测不到机型，给出提示并允许从「已注册（启用）的设备类型」下拉手动选择填充（IP 直连与设备发现两条路径均生效）
- 设备截图的 Web 预览增强与图片生命周期管理（截图可管理、可列历史、有保留策略）：
  - 新增 `ScreenshotRecord` 模型（`screenshot_record` 表）：shot_id(shot_+hex 主键) /
    device_id(可空+索引) / file_name(唯一) / bytes / quality / rotate / float_abi /
    bin / created_by(可空) / captured_at(UTC+索引)。`create_all` 自动建表，
    `migrations._ensure_screenshot_table` 用 inspect 判表做幂等兜底（新表无需 ALTER，
    缺表补建，老库升级不丢数据）
  - `app/screenshot.py` 每次成功截图后写一条记录（显式 commit），并新增领域函数
    `list_screenshots()` / `delete_screenshot()`（删 DB 行同时删文件，文件缺失不报错）
  - 新模块 `app/screenshot_lifecycle.py`：提供 serialize / stats / delete_device_screenshots /
    prune_device / prune_global；所有删除都「删 DB 行 + 删文件 + 写 audit
    （action=screenshot_prune，detail 含删除数量/原因）」
  - 在线可改配置（`config.py` 默认 + `runtime_config.py` 白名单）：
    `screenshot_max_per_device=10`、`screenshot_retention_days=14`、
    `screenshot_total_max=500`（按总条数）、`screenshot_cleanup_on_release=true`
  - 保留策略：每次截图后对该设备只保留最新 N 条（prune_device）；全局 prune 按
    captured_at 删超期、总数超上限时最旧先删（prune_global）；worker 每小时周期
    调用全局 prune（best-effort，独立任务不阻塞）
  - 换用户/释放时清理：`leasing._reclaim()` 物理闸门清理附近 best-effort 删除该
    device_id 全部截图（记录+文件+audit），try/except 包裹只记警告，绝不影响释放
    主流程（用 commit=False 挂外层事务，失败不污染释放事务）
  - REST（admin cookie）：`GET /api/screenshot-records`（按 device_id 列历史）、
    `GET /api/screenshot-records/stats`（总数/占用空间/设备数）、
    `DELETE /api/screenshot-records/{shot_id}`（单删）、
    `POST /api/screenshot-records/prune`（立即执行全局 prune）；
    `POST /api/devices/{device_id}/screenshot` 与 MCP `res_screenshot` 传 created_by
  - 前端 `web/src/App.vue`：截图对话框展示 captured_at(本地时区)/quality/rotate/
    float_abi/bin/bytes，加「下载」(`<a download>` 指向 GET 图片)、「重新截图」(loading)、
    旋转选择(0/90/180/270)；截图 URL 用 `apiUrl()` + `new URL(..., window.location.href)`
    生成绝对地址，兼容公网隧道 / Caddy 子路径；设备表加「截图记录」入口（历史缩略图 +
    点开预览 + 单张删除）；设置加「截图存储」块（总数/占用空间、保留策略表单、立即清理）

### 修复
- `app/runtime_config.py` `set_runtime_value()`：bool 值原来无论 True/False 都存成
  `"true"`，导致 `allow_apply_when_suspect` / `heartbeat_required` /
  `screenshot_cleanup_on_release` 等开关「关不掉」。改为按真实值存 true/false

### 确认（不改变默认行为）
- `app/db_source.py` 只读核对：`XOSADB2UPDATE_PATH=/home/scm/bin/xosadb2update`
  存在且可执行；`upgrade()` 默认 `update_args='-f -a -e data'`（保留 data 区）未变；
  probe/versions 只读函数可调用。未对真实设备执行升级，未改默认

### 验证
- `scripts/verify_screenshot_lifecycle.py` 对 172.16.10.246 实机截图 3 张：记录入库、
  per-device prune 只保留最新 N=2、单删/全局 prune/统计通过（临时 SQLite 库，不污染线上）
- `tests/test_screenshot_lifecycle.py` 13 项单测全通过（prune/清理/释放整合/rotate 透传）
- 全量 `pytest tests/ -q`：17 failed / 70 passed；17 项为仓库既存失败（干净 HEAD
  同名单 17/57），无新增回归；新增 13 项单测全通过
- 前端 `cd web && npm run build` 通过；Python 全模块导入 + py_compile 通过

### 备注
- 设备端 vendor 二进制 `xosscreen -r 90/180/270` 在本机实测 Segmentation fault
  （旋转是二进制自身 bug，非本系统逻辑）；实机验证统一用 rotate=0，前端仍按所选
  rotate 调用 API，rotate 取值的接收/透传由 mocked 单测覆盖

## [0.23.0] - 2026-11-09

### 新增
- 设备截图 + 芯片/浮点能力，经 MCP 释放：
  - `app/vendor/xosdbg`（软浮点 eabi，无 FPU 机型）/ `app/vendor/vxosdbg`
    （硬浮点 eabihf，带 VFP 机型）两个静态 ARM 二进制入库，保持可执行
  - 新 `app/screenshot.py`（标准库 + subprocess）：GNU `timeout` + subprocess 双保险，
    绝不无限挂起；浮点 ABI 三级判定——① 机型配置 float_abi/has_fpu →
    ② adb 读 `/proc/cpuinfo` Features（含 vfp/neon 即硬浮点）→ ③ 运行时兜底
    （分别 push 后执行 `--version`，挑可正常运行的二进制）；带截图时 ABI 不匹配自动换另一个
  - 流程：connect → 选二进制 → push `/data/xosdbg`（不可写/空间不足自动退 `/tmp/xosdbg`）
    → chmod → `xosscreen -s` 生成设备端 jpg → pull 到 `app/var/screenshots/<device>_<UTC>.jpg`
    → 校验 JPEG → 清理设备端临时文件（best-effort）
  - `prober_real.py` 探测额外读 `/proc/cpuinfo`，把 features / cpu_part /
    cpu_implementer / has_fpu 放入 `ProbeResult.extra`
  - `ProductType` 增 chip_model / float_abi(hard/soft/auto,默认 auto) / has_fpu(默认 0)，
    `models.py` + `migrations.py` inspect 判列幂等 ALTER；品类注册/编辑可填写，
    注册时从探测结果自动回填（只补空/auto，不覆盖人工设定，不阻塞注册）
  - MCP 新增 `res_screenshot`（30→31 工具，require_auth，DomainError 转 error）
  - REST 新增管理员 `POST /api/devices/{device_id}/screenshot`（cookie 鉴权）与
    `GET /api/screenshots/{file_name}`（FileResponse，白名单正则 + basename + 目录包含
    三层校验防路径穿越）
  - 前端设备表操作列加「截图」按钮（admin），成功后 el-image 弹窗预览；
    设备类型编辑/新增表单加芯片·浮点字段
  - `db_source.py`：xosadb2update 路径做成可配置常量 `XOSADB2UPDATE_PATH`
    （env 可覆盖），确认默认 `/home/scm/bin/xosadb2update` 存在且默认 `update_args`
    仍为 `-f -a -e data`（保留 data 区，不升级 data 区）

### 验证
- 对 172.16.10.246（qm10xv_full，硬浮点）截图成功：cpuinfo 判 hard → vxosdbg，
  `-s` 抓图 + pull 回主机 JPEG（SOI/EOI 可打开）；`scripts/verify_screenshot.py`
- 前端 `web/` `npm run build` 通过；`pytest tests/ -q` 全量回归通过

## [0.22.0] - 2026-11-09

### 变更
- 设备页重构为两级展示，去除「未分类」分组（无 model_class 的设备不再显示）：
  - 概况页：顶部总览统计（全部/可用/占用/离线/待检），下方按品类卡片展示各品类
    占用、可用、离线、待检数量，点击卡片进入该品类明细
  - 品类详情页：返回概况 + 品类名，状态 tab（全部/占用/可用/离线/待检）过滤，
    保留关键字搜索，表格展示单设备 ID/名称/工程配置/位置/责任人/Agent/健康/状态/ADB
- 纯前端改动，已浏览器实测：概况统计、品类卡片下钻、tab 过滤、返回均正常，无控制台报错

## [0.21.0] - 2026-11-09

### 新增
- 吸纳 dbxostest.py 核心能力，新增 app/db_source.py（标准库实现，无 CLI/邮件/报告外围）：
  - 智能适配提供的 DB 目录：支持源别名 daily/release、完整 http(s) URL、可多级 project_path
  - 探测 DB 目录可用性并判定层级（group 源根 / project 含数字版本 / version 含包）
  - 整数 DB 版本发现：指定版本优先，其次 latest/，再 HTML 解析，二分查找兜底稀疏版本
  - 列出/下载 CURRENT*.tar.gz 包、解压并定位 qmimages/script.ini
  - 读取设备当前 DB 版本（adb shell /.DB_*）
  - 经 xosadb2update 执行 DB 版本升级（默认 -f -a -e data，保留 data）
- MCP 新增 5 个工具（25→30）：res_db_probe、res_db_versions、res_db_device_version、
  res_db_prepare（下载+解压，需凭据）、res_db_upgrade（端到端升级，需凭据）
- 实测：源根 71 工程；嵌套工程 XOS_SDK_Daily/10xv_lv 版本 131，latest 包
  CURRENT-build_J131_261109.tar.gz 下载（47.7MB）、解压、script.ini 定位均通过

## [0.20.0] - 2026-11-09

### 新增
- Web 手工添加设备入口（管理员，设备页「添加设备」）：与 MCP 两段式注册同验证、同信息要求，
  支持两种模式：
  - IP 直连：指定 IP/端口探测后确认注册（POST /devices/probe、/devices/register）
  - 设备发现：集成 adbscan 扫描能力（新 app/discovery.py，自动探测本机 /24 或指定网段，
    并发扫描 5555 后回读 product_name/MAC），列表展示并标记是否已登记，选中后补充编辑再添加
- Agent 意见收集：新表 agent_feedback；MCP 新增 res_feedback（category/content），
  任何有效凭据可提交；设置内新增「Agent 意见」tab，管理员可采纳/暂缓/驳回并填写评审意见，
  支持按状态筛选，用于批判性吸收采纳

### 修复
- 凭据发放幂等：batch-generate 支持 idempotency_key，网络重试命中同一键直接返回原批次、
  不再重复建凭据；batch-send 对已发送结果做短期重放，避免响应丢失导致重复发信
- 设备页 Agent 列空白：auto-import 不写 register_agent；leasing.apply 占用时回写
  device.register_agent，并对有活跃租约但该字段为空的设备从 credential.agent_label 回填

### 清理
- 硬删快速重试产生的重复凭据 49–53（删前已备份，确认无 lease/queue 引用）
- 版本号 0.19.5 -> 0.20.0

## [0.19.5] - 2026-11-09

### 修复
- 修复「接入申请」页点击生成凭据预览无反应：预览弹窗原嵌在设置页条件块内，接入申请页不渲染；
  将预览弹窗移至 el-main 顶层，任意页面均可弹出
- 版本号 0.19.4 -> 0.19.5

## [0.19.4] - 2026-11-09

### 新增
- MCP 工具 res_roadmap：查询路线图与后续逐步释放的能力（todo/backlog），数据源为可维护的 app/roadmap.py
- 侧边栏新增「接入申请」独立页（管理员），承接发接入凭据流程

### 调整
- 信息架构整合：占用/排队/异常合并为「资源调度」子标签；审计移入「设置」子标签；
  侧边栏由 8 项收敛为 6 项（普通用户 5 项）
- 版本号 0.19.3 -> 0.19.4

## [0.19.3] - 2026-11-09

### 修复
- 修复 agent_label 回写丢失：MCP 鉴权中间件解析身份后关闭其会话，导致 credential/principal 游离；
  `_guard` 与注册包装现在用 session.merge 把身份对象并入当前会话，agent_info 标签修改可正常提交
- 版本号 0.19.2 -> 0.19.3

## [0.19.2] - 2026-11-09

### 变更
- 系统介绍内嵌于设置页时隐藏「返回 TRES 控制台」；仅独立全窗口打开 intro.html 时才显示
- 版本号 0.19.1 -> 0.19.2

## [0.19.1] - 2026-11-09

### 变更
- 系统介绍从侧边栏独立入口改为「设置」内的子标签页（iframe 内嵌 intro.html）
- 系统介绍补充总体架构分层图与详细模块图（模块职责 + 源文件路径），纯 HTML/CSS 离线可用
- 版本号 0.19.0 -> 0.19.1

## [0.16.0] - 2026-11-08

### 新增
- 运行时配置助手 `app/runtime_config.py` + `PUT /api/settings`（白名单键，DB KV 覆盖 pydantic 默认，立即生效）
- `DELETE /api/product-types/{type_code}`：无引用才物理删除；有设备/活动租约/排队引用返回 409
- 凭据 `manual_holders`：无邮箱也可生成 token（占位 `manual-<id>@local`，界面不暴露）；batch-send 跳过无邮箱条目

### 变更
- 设置页系统参数改为可编辑表单；设备类型 Tab 增加删除（二次确认）；发凭据支持「仅姓名」条目
- leasing / catalog / patrol / 凭据签发读取运行时配置
- 版本号 0.16.0

## [0.14.0] - 2026-11-08

### 新增
- 4 个固件 MCP 工具：`res_firmware_versions`（DB 版本目录）、`res_flash`（异步刷机）、`res_flash_status`（状态 + 日志尾）、`res_firmware_status`（semver 门禁）
- 刷机复用 `dbxostest.py` + `xosadb2update`：后台线程执行，`flash_job` 落库，QUEUED/RUNNING/SUCCEEDED/FAILED 状态机，日志逐 job 落盘
- 默认保留 data 分区（`preserve_data=True` → `-e data`）；清空 data 必须 `confirm_erase_data=True`
- `product_type` 增加 `db_project_name` / `required_major` / `required_minor` / `required_patch`；`device` 增加 `firmware_version`
- `res_apply` 版本门禁：major/minor 不满足跳过该设备，全部不满足报 NO_COMPATIBLE_DEVICE；未知版本警告放行；patch 落后仅带提示
- 真实设备探测（`prober_real`）读取 `/etc/fw_version`（回退 `/etc/version`），巡检回填 `device.firmware_version`

### 变更
- `product_type` 5 个真实机型回填 db_project_name：esl2 / framex / moto_ld_102d / 10xh_sdk_demo_lh / XOS_SDK_Daily/10xv_lv
- 版本号 0.14.0

## [0.13.0] - 2026-11-08

### 新增
- 发接入凭据改为两步式：先「生成凭据（预览）」调 `/api/credentials/batch-generate`，弹窗展示每人 token / 邮件正文 / MCP 配置，再「确认发送邮件」调 `/api/credentials/batch-send`
- 设备类型分「启用中 / 已停用」两区：主区只列 enabled 类型，停用类型收进折叠面板；停用走旧 disable 接口，启用走 `PUT /api/product-types/{type_code}` body `{enabled: true}`
- `/api/product-types` 列表返回 `enabled` 字段

### 变更
- `/api/version` 的 `name` 由 `Quamm RES` 改为 `Quamm TRES`，`scripts/verify_v1.py` 断言同步更新
- 版本号 0.13.0

## [0.12.0] - 2026-11-08

### 新增
- product_type 扩展 6 个工程/编译元数据字段（defconfig / sdk_repo_cmd / build_cmd / image_path / jenkins_url / notes）；幂等迁移只 ALTER ADD COLUMN
- 完整度判定：defconfig + SDK 获取命令 + 编译命令 + 镜像路径四列都非空 = 【完整】，缺任一 = 【信息不全】；允许信息不全机型存在
- Web 设置页新增「设备类型」Tab：列表打完整度标签，admin 可弹窗编辑 6 字段（PUT /api/product-types/{type_code}）
- res_apply 支持两种二选一：`product_type=`（按机型池申请 / 机型等待队列）或 `device_id=`（指定设备，兼容旧用法 target=）
- res_list_products 每个机型返回 总数 / available / leased(活跃租约) / queued(机型排队人数)
- Web 排队列表行可展开：显示 agent_label + 申请人 email/name、等待机型、已等多久

### 说明
- `/api/version` 的 `name` 当时仍为旧展示名，0.13.0 起统一为 `Quamm TRES`（verify_v1.py 断言同步更新）；版本号由 VERSION 单一真源读取
- 排队测试用例（verify_v1.py）同步改为按机型（product_type）申请，32 项验收保持全 PASS

## [0.11.0] - 2026-11-08

### 新增
- 全局改名：产品展示名 Quamm RES -> Quamm TRES（网页 title / MCP server name / README / 日志前缀 / MCP 配置 key）
- Web 登录：rr_ token 换 8 小时 HttpOnly 会话 cookie；`/api/auth/login|me|logout`；admin/user 两档权限
- 首次引导：库中无 role=admin token 时引导生成第一个管理员（已有 admin@corp.com 保留）
- 人员通讯录（contacts 表，17 人种子）+ 设置页通讯录 Tab（增删改查）
- 发接入凭据：通讯录多选 / 手填邮箱 + 权限 + 备注，后端签 rr_ token 并 SMTP 发 MCP 配置邮件
- SMTP 在线配置（settings 表）+ 发测试邮件
- 凭据台账扩展：name / role / note / 是否激活 / 创建时间；续期 / 吊销
- 租约列表显示申请人 email + name（token 关联联系人）
- user 登录后只看自己的租约 / 申请 / 凭据，看不到他人与占用详情

### 说明
- `/api/version` 的 `name` 自 0.13.0 起统一为 `Quamm TRES`；此前为兼容历史验收脚本保留旧值。

## [0.10.0] - 2026-11-08

### 新增
- 三域骨架（设备申请 / 设备注册 / 设备管理）
- 5 类机型字典 + 20 条一人一 token 凭据
- 20 台模拟设备经真实 MCP 链路注册入库
- 巡检闭环（L3/suspect/offline/恢复）与可用池口径对齐
- 17 个 MCP 工具
- Web 八个一级菜单

### 修复
- res_list_products / res_my_leases 空集合崩溃
- res_whoami is_admin 误判

### 已知限制
- 真实 ADB 探测未接入（仅 mock 后端）
- 串口资源未纳入
- 通知仅落库，未接企微/邮件
