# 领域约定

定义基层上转、现场分级、院内协调、下转和回访之间的闭环事件。

聚合对象包括`referral_order`、`arrival_assessment`、`care_coordination`、`return_plan`。事件类型包括`REFERRAL_RECEIVED`、`ARRIVAL_CONFIRMED`、`URGENCY_ESCALATED`、`RESOURCE_BOOKED`、`FOLLOWUP_COMPLETED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ARRIVAL_CONFIRMED`：载荷还需包含 `observed_at`, `assessor_ref`。
- `URGENCY_ESCALATED`：载荷还需包含 `reason`, `review_due_at`。
- `FOLLOWUP_COMPLETED`：载荷还需包含 `outcome`, `next_action`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
