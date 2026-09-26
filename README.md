# 县域转诊陪护闭环簿

定义基层上转、现场分级、院内协调、下转和回访之间的闭环事件，并在契约之上提供闭环服务：角色分权确认、业务键幂等、冲突隔离、危急升级限期补证、资源原子安排与冻结释放、可控时钟期限、患者与分权视图、审计还原。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/referral_concierge/contracts.py`：基础契约校验。
- `src/referral_concierge/service.py`：闭环服务（命令、幂等、冲突、状态推进）。
- `src/referral_concierge/resources.py`：检查窗口与陪护人员的原子预订和冻结规则。
- `src/referral_concierge/scheduler.py`：接站、检查、复核、下转、复查、回访期限待办。
- `src/referral_concierge/clock.py`：可控时钟（系统/手动）。
- `src/referral_concierge/views.py`：患者下一步与分权记录视图。
- `src/referral_concierge/audit.py`：审计还原。
- `src/referral_concierge/cli.py`：命令行入口。
- `tests/`：信封、时间、版本、事件载荷与闭环服务测试。
- `docs/domain.md`：领域对象、事件语义与服务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m referral_concierge.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 审计还原

```bash
PYTHONPATH=src python3 -m referral_concierge.cli audit <数据目录> <case_id>
```

还原一次上转为何升级、资源如何协调、何时下转以及康复指导是否真正接续。
