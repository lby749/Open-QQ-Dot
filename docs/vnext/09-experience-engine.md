# Phase 4 — ExperienceEngine 复用与只读投影

日期：2026-10-30  
状态：**复用现有 TaskExperienceStore 完成只读经验投影；没有另写一套状态机。**

## 关键判断

仓库里已经存在：

- `local_retrieval_engine.TaskExperienceStore`
- 状态机 `CANDIDATE → EXECUTED → RESULT_RECORDED → VALIDATED`
- HMAC 验证签名
- `experience_audit_logs`
- 真实 TaskManager 任务校验
- 混合检索与跨模型向量检查

所以没有重复实现一套“ExperienceEngine”；新增的是 vNext 只读适配层。

## 新增

`experience_service.py`：

- 读取 `data/retrieval_engine.sqlite3`
- 统计 total / VALIDATED / high_verified / CANDIDATE / FAILED / PARTIAL
- 返回最近经验摘要（problem/action 截断到 180 字）
- 数据库缺失时 `available=false`
- 不加载 BGE-M3，不触发模型下载

Bridge：

```text
GET /api/experiences
GET /api/experiences/search?q=巡检&owner=system:goal_patrol
```

桌面“记忆与反思”页已接真实数据：

![经验只读页](<D:\Desktop\New folder (3)\Open-QQ-Dot\build\desktop-experience.png>)

真实当前结果：

```json
{
  "available": true,
  "total": 2,
  "verified": 2,
  "by_state": {"VALIDATED": 2},
  "by_confidence": {"high_verified": 2}
}
```

这两条是现有系统真实的长期目标巡检和配置巡检经验，不是 demo 数据。

## 测试

```text
tests/test_experience_service.py   2 passed
tests/test_vnext_bridge.py         3 passed
```

测试覆盖：

- 从 SQLite 读取真实状态与 verified 数量
- 缺失数据库明确返回不可用
- bridge 路由

## 搜索

已接真实 FTS5 + 中文子串回退。实际查询 `巡检` 返回两条 VALIDATED 经验，不是 demo。

## 未实现

- 通过新 UI 写入/验证/淘汰经验
- 跨模型 A/B 自动实验
- 经验有效性周期检查
- 冲突经验合并
- 手机端经验查询

这些仍由现有原模块/后续阶段负责；本页明确标注“只读投影”。

## 下一步

1. 增加 `GET /api/experiences/search?q=`，调用现有 `TaskExperienceStore.search_experiences`。
2. 增加经验审计日志只读视图。
3. 在真实任务完成后补一个跨模型 A/B 实验记录。
