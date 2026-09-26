# 领域约定

定义基层上转、现场分级、院内协调、下转和回访之间的闭环事件。

聚合对象包括`referral_order`、`arrival_assessment`、`care_coordination`、`return_plan`。事件类型包括`REFERRAL_RECEIVED`、`ARRIVAL_CONFIRMED`、`URGENCY_ESCALATED`、`RESOURCE_BOOKED`、`FOLLOWUP_COMPLETED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ARRIVAL_CONFIRMED`：载荷还需包含 `observed_at`, `assessor_ref`。
- `URGENCY_ESCALATED`：载荷还需包含 `reason`, `review_due_at`。
- `FOLLOWUP_COMPLETED`：载荷还需包含 `outcome`, `next_action`。

## 闭环服务

`src/referral_concierge/service.py` 在契约之上负责业务幂等、冲突隔离和状态推进，贯通基层转诊单、前置联系、到达计划、现场快速观察、专科与检查资源、陪护责任、住院或离院决定、下转方案、复查清单和回访结果。

### 角色分权

- 基层医生（`primary_doctor`）确认来源事实：登记转诊单、回填复查结果。
- 转诊管家（`concierge`）确认现场情况：前置联系、到院观察、资源安排、释放预约、回访。
- 专科医生（`specialist`）确认临床处置：补全升级依据、住院或离院决定、下转方案。

任何角色都不能替其他角色补写结论，越权命令直接拒绝且不留痕；已登记的确认不可改写。

### 幂等与冲突

每条命令携带业务键。相同业务键且内容一致的消息沿用首次回执，离线乱序重放安全；相同业务键内容不一致，或病情、时间、患者身份与已登记事实冲突时，登记冲突条目，绝不自动合并。

### 危急升级

升级登记后立即占用最小必要资源（`min_slots`），并在 `review_due_at` 前由专科医生补全临床依据；超期未补全会在审计与到期待办中显形。

### 资源与冻结

检查窗口与陪护人员按槽位登记，一次预订全有或全无；槽位开场前进入冻结期（默认 30 分钟），冻结中的预约不可释放。患者未到（`no_show`）或需求变化（`needs_changed`）时，只释放未使用且未冻结的预约。

### 时钟与待办

接站、检查、升级复核、下转、复查、回访期限由可控时钟驱动，待办持久化在服务目录（`ledger.jsonl`、`todos.json`、`resources.json`），重启后 `Service.open` 重放台账继续待办。

### 视图与审计

- `views.patient_view`：患者看到下一步、责任联系人和相关期限。
- `views.record_view`：`primary_clinic` 看到来源到回访的连续链条，`county_hospital` 看到完整记录，`auditor` 额外看到冲突条目。
- `audit.build_audit` / `render_audit`：还原一次上转为何升级、资源如何协调、何时下转以及康复指导是否真正接续；命令行 `python -m referral_concierge.cli audit <数据目录> <case_id>`。
