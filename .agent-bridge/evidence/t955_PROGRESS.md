# Quamm TRES 实现进度

> 任务：t955 —— COMRes 节点视图硬删 500 + 权限提示失真  
> task_id: t955

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| 一 delete_node 级联 | 清空 `wl_client_apply.issued_node_id/token_cipher`，保留行；cascade 含 `applies` | ✅ |
| 二 IntegrityError→409 | `WL_DELETE_CONFLICT`，detail 列残留表名 | ✅ |
| 三 前端 | 删除按钮仅 admin；toast 展示后端 detail/message | ✅ |
| 四 再申请 | 节点硬删后同 client_id 重新签发 | ✅ |
| 五 pytest | `tests/test_wirelog_delete_node.py` 4 例 + apply 回归 11 passed | ✅ |
| 六 真库 | `wlf4fd4fc2fed8ceae` 已删，剩 2 节点；apply token 已清空 | ✅ |

## 提交

- `0f5f9e0` / `0f5f9e09c9b31ee317d1ba32286e257bf0d210ee`

## 说明

- 停住等审
