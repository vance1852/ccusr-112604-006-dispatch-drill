"""把合成资料时间线驱动到演练控制服务。"""

import json
from pathlib import Path


def load_inventory(path):
    """读取清册（资源、对象与约定）。"""
    with Path(path).open(encoding="utf-8") as source:
        inventory = json.load(source)
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError("清册应为非空对象")
    return inventory


def load_timeline(path):
    """逐行读取事件时间线，校验接收时刻顺序。"""
    path = Path(path)
    previous = 0
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("事件应为对象")
                if type(record.get("at")) is not int or record["at"] < previous:
                    raise ValueError("接收时刻应为顺序排列的非负整数")
                kind = record.get("kind")
                if not isinstance(kind, str) or not kind:
                    raise ValueError("缺少事件类别")
            except ValueError as error:
                raise ValueError(f"{path.name}:{number} {error}") from error
            previous = record["at"]
            yield record


def replay(service, records):
    """按接收顺序把事件送入服务，返回每步的处置结果。"""
    outcomes = []
    for record in records:
        at = record["at"]
        kind = record["kind"]
        if kind == "run_start":
            outcome = service.start_run(
                record["run"], record["team"], record.get("snapshot", "default"), at=at
            )
        elif kind == "call":
            outcome = service.call(
                record["run"], record["ticket"], record["action"],
                record["operation"], record["endpoint"], at=at,
            )
        elif kind == "receipt":
            outcome = service.receive_receipt(
                record.get("run"), record["ticket"], record["operation"],
                record["namespace"], record["endpoint"], record["state"], at=at,
            )
        elif kind == "supersede":
            outcome = service.supersede(
                record["incident"], record["supervisor"], record["scopes"], at=at
            )
        elif kind == "handoff":
            outcome = service.handoff(
                record["incident"], record["ticket"], record["scope"],
                record["operation"], at=at,
            )
        elif kind == "resume":
            if "same_run" not in record:
                raise ValueError("resume 记录缺少 same_run，须明确接续或再开")
            outcome = service.resume(
                record["run"], record["from_run"],
                snapshot=record.get("snapshot", "default"),
                same_run=bool(record["same_run"]), at=at,
            )
        else:
            outcome = {"ignored": f"未知事件类别 {kind}"}
        outcomes.append({"at": at, "kind": kind, "record": record, "outcome": outcome})
    return outcomes
