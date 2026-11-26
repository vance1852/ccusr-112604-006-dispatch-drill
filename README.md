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
