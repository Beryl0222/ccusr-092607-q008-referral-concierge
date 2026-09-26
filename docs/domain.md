# 领域约定

定义基层上转、现场分级、院内协调、下转和回访之间的闭环事件。所有发生时间都必须携带时区，版本号按 `aggregate_id`（即转诊案例号）从 1 开始递增，基础校验不会改写调用方输入。

## 聚合与事件

聚合对象包括 `referral_order`（转诊单与到院前联系）、`arrival_assessment`（到院与现场评估）、`care_coordination`（院内资源与陪护协调）、`return_plan`（下转与康复接续）。

| 事件 | 聚合 | 语义 |
| --- | --- | --- |
| `REFERRAL_RECEIVED` | referral_order | 基层转诊单接收，登记患者身份、来源机构与电话描述病情 |
| `PREARRIVAL_CONTACTED` | referral_order | 前置联系患者，确认需求与预计到达 |
| `ARRIVAL_PLANNED` | referral_order | 到达计划：接站点、接站人与接站期限 |
| `ARRIVAL_CONFIRMED` | arrival_assessment | 患者实际到院确认 |
| `OBSERVATION_RECORDED` | arrival_assessment | 现场快速观察与分级（routine/urgent/critical） |
| `URGENCY_ESCALATED` | arrival_assessment | 危急升级，可先占用最小必要资源，限期补全依据 |
| `ESCALATION_JUSTIFIED` | arrival_assessment | 专科医生在期限内补全升级依据 |
| `RESOURCE_BOOKED` | care_coordination | 检查窗口/陪护人员预约（一次事件原子生效） |
| `RESOURCE_RELEASED` | care_coordination | 预约释放：未到、需求变化、被抢占、占用超期 |
| `ESCORT_ASSIGNED` | care_coordination | 陪护责任与责任联系人 |
| `DISPOSITION_DECIDED` | care_coordination | 住院或离院决定 |
| `DOWNWARD_PLANNED` | return_plan | 下转方案与交接期限 |
| `DOWNWARD_HANDOVER_CONFIRMED` | return_plan | 下转交接完成确认 |
| `REVIEW_CHECKLIST_ISSUED` | return_plan | 复查清单与回访期限 |
| `FOLLOWUP_COMPLETED` | return_plan | 回访结果，核销复查项目 |
| `DEADLINE_BREACHED` | care_coordination | 可控时钟发现的期限违约（接站/升级依据/检查/下转/复查/回访/绿色通道占用） |

## 角色分离

- 基层医生（`primary_doctor`）确认来源事实：转诊单、患者身份、电话描述病情。
- 转诊管家（`concierge`）确认现场情况：前置联系、到达计划、到院确认、现场观察、陪护安排、交接确认与回访。
- 专科医生（`specialist`）确认临床处置：升级依据、住院或离院决定、下转方案、复查清单。

任何人都不能替其他角色补写结论：服务按命令校验角色，并拒绝携带越权结论字段的载荷（例如现场观察不得夹带处置决定）。危急升级可由转诊管家或专科医生发起，但升级依据只能由专科医生补全。

## 幂等、冲突与乱序

- 每条命令携带业务键 `business_key`；相同业务键且内容一致的命令沿用首次回执，不重复入账（回执崩溃丢失时从事故日志中的 `content_hash` 重建）。
- 相同业务键但病情、时间或患者身份等内容不一致时登记冲突并拒绝，绝不自动合并，原事实保持不变。
- 离线消息可以乱序到达：前置事实未就绪的命令进入待办队列，前置事件入账后按到达顺序自动补放；重启后待办继续有效。

## 危急升级与资源

- 升级事件携带 `review_due_at`；允许先占用最小必要资源（`hold_kind=escalation_minimal`），专科医生须在期限内补全依据，逾期由时钟释放占用并登记违约。
- 患者未到（接站期限过后未到院）或需求变化时，释放尚未使用的预约。
- 普通案例占用绿色通道资源设有最长持有期，超期由时钟释放，防止普通陪诊长期占用稀缺通道；已升级案例不受此限。
- 多个患者争用检查窗口和陪护人员时按冻结规则原子安排：一次预约命令要么全部资源入账、要么全部失败；窗口开始前进入冻结期的预约不可被抢占；危急案例可抢占普通案例中未冻结的预约，被抢占预约登记释放并等待重新安排。

## 可控时钟

时钟负责接站、检查、升级依据、下转、复查和回访期限。`tick(now)` 幂等推进：同一期限只登记一次违约；重启后从事件日志重建全部待办，继续计时。时钟不得回拨。

## 可见性与审计

- 患者视图：下一步动作与责任联系人，不泄露临床细节。
- 基层视图：来源事实、流转状态、下转与回访结果等连续记录，不含院内资源调度细节。
- 县医院视图：完整时间线，含升级依据、预约与违约。
- 审计命令：还原一次上转为何升级、资源如何协调、何时下转、康复指导是否真正接续，并附冲突记录。
