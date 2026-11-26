"""命令行：回放演练时间线，察看隔离、中断、接管与偏差。

用法：
    python3 -m drill_control samples/shift-log.jsonl --db /tmp/drill.db --fresh
"""

import argparse
import json
from pathlib import Path

from .replay import load_inventory, load_timeline, replay
from .service import DrillControlService

_FLAG_NAMES = {
    "orphan": "孤儿回执",
    "duplicate": "重复回执",
    "after_revoke": "收回后完成",
    "misdelivered_call": "错投指令",
}


def _dump(data):
    return json.dumps(data, ensure_ascii=False, default=str)


def _print_steps(outcomes):
    print("== 回放过程 ==")
    for step in outcomes:
        record = step["record"]
        outcome = step["outcome"]
        kind = step["kind"]
        if kind == "run_start":
            line = f"轮次 {outcome['run_id']} 已开始（班组 {outcome['team']}，起点 {outcome['snapshot']}）"
        elif kind == "call":
            if outcome.get("misdelivered"):
                line = (f"指令 {outcome['operation']} 错投实际业务替身 "
                        f"{outcome['endpoint']}（可观测）")
            elif outcome["accepted"]:
                line = (f"指令 {outcome['operation']} 已发往 {outcome['endpoint']}"
                        f"（{outcome['run']} / {outcome['ticket']}）")
            else:
                line = f"指令 {outcome['operation']} 被拒：{outcome['reason']}"
        elif kind == "receipt":
            flag = outcome["flag"]
            note = f"，{_FLAG_NAMES.get(flag, flag)}" if flag else ""
            where = "实际业务" if outcome["namespace"] == "live" else f"轮次 {outcome['run']}"
            line = f"{record['endpoint']} 回执 {outcome['operation']}={record['state']}（{where}{note}）"
        elif kind == "supersede":
            owed = [f"{c['run_id']}:{c['operation']}" for c in outcome["in_flight"]]
            line = (f"主管收回范围 {outcome['scopes']}（{outcome['incident']}），"
                    f"在途待收尾：{owed or '无'}")
        elif kind == "handoff":
            if outcome["accepted"]:
                cred = outcome["credential"]
                line = (f"接管成立：{outcome['scope']} 凭实际业务端证据 "
                        f"{cred['endpoint']}#{cred['receipt_id']}")
            else:
                line = f"接管被拒：{outcome['reason']}"
        elif kind == "resume":
            mode = "接续原轮次" if outcome["same_run"] else "从新起点再开"
            line = (f"恢复演练：{outcome['run']}（{mode}，解除范围 "
                    f"{outcome['lifted_scopes'] or '无'}）")
        else:
            line = _dump(outcome)
        print(f"  at={step['at']:<3} {kind:<10} {line}")


def _print_observations(service):
    print("\n== 两套替身各自收到的操作 ==")
    for namespace, inboxes in service.standin_overview().items():
        label = "演练端" if namespace == "drill" else "实际业务端"
        for endpoint, ops in inboxes.items():
            entries = [
                f"{op['operation']}({op['run_id'] + '，' if op['run_id'] else ''}"
                f"{op['action']}→{op['status']}"
                f"{'，错投' if op['misdelivered'] else ''})"
                for op in ops
            ]
            print(f"  [{label}] {endpoint}: {entries or '（无）'}")

    print("\n== 异常与错投 ==")
    violations = service.violations()
    if not any(violations.values()):
        print("  （无）")
    for call in violations["misdelivered_calls"]:
        print(f"  错投：{call['run_id']} 的 {call['operation']} 发往 {call['endpoint']}")
    for call in violations["rejected_calls"]:
        print(f"  被拒指令：{call['run_id']} 的 {call['operation']}（{call['reason']}）")
    for receipt in violations["flagged_receipts"]:
        print(f"  异常回执：{receipt['operation']}（{_FLAG_NAMES.get(receipt['flag'], receipt['flag'])}）")
    for handoff in violations["rejected_handoffs"]:
        print(f"  被拒接管：{handoff['incident']} {handoff['scope']}（{handoff['reason']}）")

    print("\n== 中断与接管 ==")
    for item in service.interruptions():
        state = "进行中" if item["active"] else f"已于 at={item['lifted_tick']} 解除"
        print(f"  {item['incident']}：收回 {item['scopes']}（{state}）")
    for item in service.takeovers():
        if item["status"] == "accepted":
            cred = item["credential"]
            print(f"  接管 {item['scope']}：凭据 {cred['namespace']}/{cred['endpoint']}"
                  f" 回执#{cred['receipt_id']}（{cred['state']}）")
        else:
            print(f"  接管 {item['scope']} 被拒：{item['reason']}")

    print("\n== 各班处置结果 ==")
    runs = service.list_runs()
    for run in runs:
        results = service.run_results(run["run_id"])
        tickets = {
            ticket: info["state"]
            for ticket, info in results["tickets"].items()
        }
        print(f"  {run['run_id']}（{run['team']}）：{_dump(tickets)}"
              f"{'，全部处置完成' if results['all_handled'] else ''}")

    teams = {}
    for run in runs:
        teams.setdefault(run["team"], run["run_id"])
    pair = list(teams.values())
    if len(pair) >= 2:
        compared = service.compare_runs(pair[0], pair[1])
        print(f"\n== 班组偏差对照（{pair[0]} vs {pair[1]}）==")
        if not compared["deviations"] and not compared["state_diffs"]:
            print("  两班过程一致，无偏离")
        for deviation in compared["deviations"]:
            print(f"  工单 {deviation['ticket']}：{pair[0]}={deviation[pair[0]]}，"
                  f"{pair[1]}={deviation[pair[1]]}")
        for ticket, states in compared["state_diffs"].items():
            print(f"  工单 {ticket} 末态：{_dump(states)}")


def main():
    parser = argparse.ArgumentParser(
        prog="drill_control", description="回放演练时间线并察看隔离、中断、接管与偏差"
    )
    parser.add_argument("timeline", type=Path, help="演练事件文件")
    parser.add_argument("--db", type=Path, default=Path("drill-control.db"),
                        help="演练库（SQLite），默认 ./drill-control.db")
    parser.add_argument("--fresh", action="store_true", help="先删除已有演练库")
    args = parser.parse_args()
    try:
        if args.fresh and args.db.exists():
            args.db.unlink()
        inventory = load_inventory(args.timeline.parent / "inventory.json")
        with DrillControlService(str(args.db), inventory) as service:
            outcomes = replay(service, load_timeline(args.timeline))
            _print_steps(outcomes)
            _print_observations(service)

        print("\n== 重启后查询（重新打开演练库）==")
        with DrillControlService(str(args.db)) as reopened:
            rounds = [f"{r['run_id']}({r['team']})" for r in reopened.list_runs()]
            print(f"  轮次：{rounds}")
            held = [f"{i['incident']}:{i['scopes']}" for i in reopened.interruptions()]
            print(f"  被中断的范围：{held or '（无）'}")
            taken = [f"{t['scope']}←回执#{t['credential_receipt_id']}"
                     for t in reopened.takeovers() if t["status"] == "accepted"]
            print(f"  接管凭据：{taken or '（无）'}")
    except (OSError, ValueError) as error:
        parser.exit(2, f"演练控制失败：{error}\n")


if __name__ == "__main__":
    main()
