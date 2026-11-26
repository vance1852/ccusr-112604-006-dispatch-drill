# 协同助手值班演练的隔离与真实接管

这里保存一套可脱机使用的合成运维资料。两个事件文件各自从相同清册开始，模拟两次独立运行。

## 清册和事件

`samples/inventory.json` 记录演练开始时已知的资源、对象与约定。endpoints 只列本地替身的逻辑地址。snapshot 是演练起点的只读业务副本，namespace 分为 drill/live。resources 的额度只约束对应演练范围内的实际替身动作。

`samples/shift-log.jsonl` 和 `samples/partial-resume.jsonl` 每行是一个 JSON 对象。通用字段 `at` 是资料到达值班侧的演练秒，`kind` 标明来源记录的类型。同一秒内保持给定文件顺序。事实发生时间由记录自身字段提供。at 是表单或替身回执进入控制台的顺序时刻。run_start 为新演练轮次；call 表明发出的可追踪意图；receipt 包括实际到达的 namespace 与 run，真实业务不提供 run。supersede 的 scopes 由主管收回，resume 中 from_run 可作为原轮次的继续依据，或指定已知起点开新轮次。

文件可包括申请、已发指令、状态观测和外部回执。出现于记录中的申请或意图可能未获准，外部回执可能迟到或与此前观测冲突。

## 浏览资料

在仓库根目录用 Python 3.11 运行。只需标准库。

```sh
python3 sample_reader.py samples/shift-log.jsonl
python3 -m unittest discover -s tests -v
```

原始资料可按 `--kind` 筛选。程序按接收顺序显示记录。单元测试核对资料能够读取，以及事件夹具的基本结构。

## 演练控制服务

`drill_control` 包在本地替身之上实现演练控制，全部状态存入 SQLite，重启后仍可查询。

```sh
python3 -m drill_control samples/shift-log.jsonl --db /tmp/shift.db --fresh
python3 -m drill_control samples/partial-resume.jsonl --db /tmp/partial.db --fresh
```

主管通过 `DrillControlService` 编程操作：

- `start_run` / `call`：开启班组轮次并发出派单、确认、撤回、通知。轮次从清册快照（生产业务的合成副本）出发，指令只在本轮次内有效；通知受清册 `simultaneous_notices` 额度约束。
- `receive_receipt` / `settle` / `finalize_pending`：登记替身回执、让替身上待收尾的操作产生回执。演练回执按（轮次, 操作名）严格归账，旧轮次回执不会归给新一轮同名请求；实际业务回执不提供轮次，只作为接管证据留存。
- `supersede`：真实告警插入时收回特定范围。暂停只停止新动作，已发给替身的动作列入在途清单，可经回执查证（带"收回后完成"标记）或受控收尾；未受影响范围继续演练。
- `handoff`：办理实际业务接管。凭据必须来自实际业务端（live 命名空间回执）并覆盖主管指定的范围；替身成功回执与演练生成的交接材料仅供复盘，不能作为凭据。
- `resume`：恢复被收回的范围。`same_run=True` 接续原轮次，`same_run=False` 从已知起点再开一轮；恢复前报告上次中断以来已经改变的对象。
- `standin_inbox` / `standin_overview` / `violations`：察看演练端与实际业务端两套替身各自收到的操作，错投、被拒指令、异常回执、被拒接管均可观测。
- `run_results` / `compare_runs`：各班处置结果按各自轮次独立判定，另一组先完成的处置不计入这一组；两班复用同一场景时可对照过程偏离。
- `list_runs` / `interruptions` / `takeovers` / `review_materials`：轮次、被中断的范围、接管凭据与复盘材料，重启后仍可查询。

命令行回放会打印每步处置结果、两套替身收件箱、异常与错投、中断与接管、各班结果与班组偏差对照，最后重新打开演练库演示重启后查询。
