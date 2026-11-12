# Phase 3 — ExperienceEngine 候选写入

日期：2026-10-30  
状态：**用户候选经验可写入，强制 CANDIDATE / low_unverified；验证成功入口未开放。**

## 设计

新增：

```text
POST /api/experiences/candidate
```

只接受：

```json
{"problem":"...", "action":"...", "cause":"..."}
```

调用现有 `TaskExperienceStore.record_experience`，参数：

```text
result_status = unverified_suggestion
task_id = ""
provider = user
owner = user
```

因此状态机强制：

```text
lifecycle_state = CANDIDATE
confidence = low_unverified
```

不会伪造：

```text
VALIDATED
success_verified
high_verified
```

## 真实 HTTP 测试

在临时复制经验库上启动 Bridge：

```text
POST /api/experiences/candidate
-> {"id":"exp_2836709_9dd143","state":"CANDIDATE",
    "confidence":"low_unverified","note":"用户候选，未经验证；不能作为已验证成功"}
```

随后：

```text
GET /api/experiences
-> by_state: {"CANDIDATE":1,"VALIDATED":2}
```

原有两条 `VALIDATED` 记录未被修改；新增记录只是候选。

临时库在测试后删除，没有污染真实数据。

## 测试

```text
tests/test_experience_service.py   4 passed
```

其中新增测试断言：

- state == CANDIDATE
- confidence == low_unverified
- note 明确“未经验证”

## 未完成

- 桌面“记忆与反思”页的候选表单
- 候选→验证的真实任务关联
- 经验淘汰/合并/A-B
- 用户候选的编辑/删除
- 真实成功率评估

验证成功仍必须经过原 `TaskExperienceStore` 的任务与 Verifier 校验，Bridge 不提供绕过入口。
