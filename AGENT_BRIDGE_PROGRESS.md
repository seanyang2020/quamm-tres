# Quamm TRES 实现进度

> 任务：t976 —— 客户端实例下载（standalone / ELSB 安装包分发）  
> task_id: t976

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| 一 模型/迁移 | `wl_package` + 幂等 `_ensure_wl_package_table` | ✅ |
| 二 存储 | `<data_dir>/packages/<id>-<filename>` 原子写入；文件名清洗；默认 200MB | ✅ |
| 三 API | GET/POST/DELETE + download；可选 PUT channel；审计 `comres_package_*` | ✅ |
| 四 前端 | 菜单「客户端下载」；上传（admin）/列表/直链下载/删除 | ✅ |
| 五 单测/真机 | pytest 7 passed；真机上传→下载+1→user 403→清理 | ✅ |

## 提交

- 功能：`f78b539` / `f78b539a664bfd771f77d51bcb93f6a82cc235cb`
- 证据：`8a71734` / `8a717348ff2f8b31c95f8cb241530d24ee6dd200`

## 说明

- 停住等审
