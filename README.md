# 县域转诊陪护闭环簿

定义并实现基层上转、现场分级、院内协调、下转和回访之间的闭环服务：贯通基层转诊单、前置联系、到达计划、现场快速观察、专科与检查资源、陪护责任、住院或离院决定、下转方案、复查清单和回访结果。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `data/demo/`：演练用资源台账与调度策略。
- `src/referral_concierge/`：
  - `contracts.py`：领域事件信封校验（不改写输入）。
  - `model.py`：角色、事件类型、命令规格与调度策略。
  - `clock.py`：可控时钟（系统/手动）。
  - `store.py`：追加式事件日志与回执、冲突、待办持久化。
  - `projection.py`：从事件重建案例状态与资源台账。
  - `service.py`：闭环服务核心（幂等、冲突、乱序、角色、升级、释放、时钟）。
  - `resources.py`：检查窗口与陪护人员的原子预约、冻结与抢占。
  - `views.py`：患者/基层/县医院三级权限视图。
  - `audit.py`：审计还原（为何升级、资源协调、下转时间、康复接续）。
  - `cli.py`：命令行入口。
- `scripts/run_demo.py`：两个案例的端到端演练。
- `tests/`：契约、服务、资源、视图、审计与 CLI 测试。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 核心规则

- **角色分离**：基层医生确认来源事实，转诊管家确认现场情况，专科医生确认临床处置；任何人不能替其他角色补写结论（越权字段直接拒绝）。
- **业务键幂等**：相同业务键内容一致沿用首次回执；回执丢失时从事故日志的 `content_hash` 重建。
- **冲突不合并**：相同业务键但病情、时间或患者身份不一致时登记冲突并拒绝，原事实保持不变。
- **乱序重放**：前置事实未到的命令进入待办队列，前置入账后自动补放，重启后继续。
- **危急升级**：可先占用最小必要资源，专科医生须在 `review_due_at` 前补全依据，逾期时钟释放占用并登记违约。
- **冻结规则**：多资源预约全有或全无；窗口开始前进入冻结期的预约不可抢占；危急案例可抢占普通案例未冻结的预约。
- **稀缺通道**：普通案例占用绿色通道资源有最长持有期，超期由时钟释放。
- **可控时钟**：`tick` 幂等处理接站、升级依据、检查、下转、复查、回访期限；时钟不得回拨；重启后从事件日志继续待办。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests scripts
```

## 样例校验（旧用法保留）

```bash
PYTHONPATH=src python3 -m referral_concierge.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 闭环簿用法

```bash
# 初始化闭环簿（资源台账 + 调度策略）
PYTHONPATH=src python3 -m referral_concierge.cli init board \
  --resources data/demo/resources.json --policy data/demo/policy.json

# 应用一条命令（命令 JSON 见 scripts/run_demo.py 中的构造）
PYTHONPATH=src python3 -m referral_concierge.cli apply board command.json --now 2026-09-25T08:00:00+08:00

# 推进可控时钟，处理到期事项
PYTHONPATH=src python3 -m referral_concierge.cli tick board --now 2026-09-25T12:00:00+08:00

# 按权限查看连续记录：patient | primary | hospital
PYTHONPATH=src python3 -m referral_concierge.cli view board ZZ-2026-0001 --as patient

# 审计还原一次上转
PYTHONPATH=src python3 -m referral_concierge.cli audit board ZZ-2026-0001

# 查看乱序待办、内容冲突与资源台账
PYTHONPATH=src python3 -m referral_concierge.cli pending board
PYTHONPATH=src python3 -m referral_concierge.cli conflicts board
PYTHONPATH=src python3 -m referral_concierge.cli ledger board
```

## 端到端演练

```bash
PYTHONPATH=src python3 scripts/run_demo.py
```

演练覆盖：危急升级与最小必要资源占用、限期补全依据、冻结规则下的抢占、幂等重放、内容冲突、乱序待办、模拟重启、下转交接、复查与回访期限违约、康复接续判定，并输出患者视图、基层视图与审计报告。
