"""演练控制服务的核心逻辑。

一次演练（轮次）从清册快照这份生产业务合成副本出发，各班在各自轮次内
推进派单、确认与撤回；通知、工单、接管各有演练端与实际业务端两套本地
替身，错投可被观测；真实告警插入时主管可收回部分范围、凭实际业务端
证据办理接管，并选择接续原轮次或从新起点再开一轮。所有状态持久化在
SQLite 中，重启后仍可查询。
"""

import json

from .standins import StandInNetwork
from .store import Store

# 实际业务端可以作为接管凭据的回执状态
_EVIDENCE_STATES = ("accepted", "done")
# 演练内动作对工单状态的影响（仅在替身回执成功后生效）
_TICKET_TRANSITIONS = {
    "assign": "assigned",
    "confirm": "confirmed",
    "cancel": "cancelled",
    "withdraw": "open",
}


def _validate_inventory(inventory):
    if not isinstance(inventory, dict):
        raise ValueError("清册应为对象")
    endpoints = inventory.get("endpoints")
    if (
        not isinstance(endpoints, dict)
        or not endpoints.get("drill")
        or not endpoints.get("live")
    ):
        raise ValueError("清册应提供 drill/live 两套替身地址")
    overlap = set(endpoints["drill"]) & set(endpoints["live"])
    if overlap:
        raise ValueError(f"替身地址在两套间重复：{sorted(overlap)}")
    snapshot = inventory.get("snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        raise ValueError("清册应包含非空快照")
    for row in snapshot:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) for key in ("ticket", "scope", "state")
        ):
            raise ValueError("快照条目应包含 ticket/scope/state")
    for resource in inventory.get("resources", []):
        if (
            not isinstance(resource, dict)
            or not isinstance(resource.get("scope"), str)
            or type(resource.get("simultaneous_notices")) is not int
            or resource["simultaneous_notices"] < 0
        ):
            raise ValueError("资源条目应包含 scope 与非负 simultaneous_notices")


class DrillControlService:
    """值班演练控制服务。

    主管通过它启动与推进班组演练、发起部分中断并办理接管、察看两套
    替身各自收到的操作，并用同一份场景再开一轮。
    """

    def __init__(self, db_path, inventory=None):
        self.store = Store(db_path)
        stored = self.store.get_meta("inventory")
        if inventory is None:
            if stored is None:
                self.store.close()
                raise ValueError("首次打开演练库必须提供清册")
            inventory = json.loads(stored)
        else:
            _validate_inventory(inventory)
            encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True)
            if stored is not None and stored != encoded:
                self.store.close()
                raise ValueError("清册与已存演练库不一致")
            self.store.set_meta("inventory", encoded)
        self.inventory = inventory
        self.snapshots = {"default": list(inventory["snapshot"])}
        self.quotas = {
            row["scope"]: row["simultaneous_notices"]
            for row in inventory.get("resources", [])
        }
        self._scopes = {row["ticket"]: row["scope"] for row in inventory["snapshot"]}
        self.standins = StandInNetwork(self.store, inventory["endpoints"])
        self._tick = int(self.store.get_meta("tick", "0"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.store.close()

    # ------------------------------------------------------------------
    # 内部工具

    def _clock(self, at):
        """推进逻辑时钟。回放时同一秒的多条记录共享同一刻度。"""
        if at is None:
            self._tick += 1
        else:
            if type(at) is not int or at < 0:
                raise ValueError("接收时刻应为非负整数")
            self._tick = max(self._tick, at)
        self.store.set_meta("tick", str(self._tick))
        return self._tick

    def _run(self, run_id):
        return self.store.one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def _ticket_scope(self, ticket):
        return self._scopes.get(ticket)

    def _blocking_incident(self, scope):
        for row in self.store.all(
            "SELECT * FROM interruptions WHERE lifted_tick IS NULL ORDER BY tick"
        ):
            if scope in json.loads(row["scopes"]):
                return row["incident"]
        return None

    def _is_handed_over(self, scope):
        return (
            self.store.one(
                "SELECT handoff_id FROM handoffs WHERE scope=? AND status='accepted'",
                (scope,),
            )
            is not None
        )

    def _crossed_interruption(self, scope, start_tick, end_tick):
        """指令在 (start, end] 期间是否穿越过针对其范围的收回。"""
        for row in self.store.all(
            "SELECT * FROM interruptions WHERE tick > ? AND tick <= ?",
            (start_tick, end_tick),
        ):
            if scope in json.loads(row["scopes"]):
                return True
        return False

    def _reject_call(self, run_id, ticket, action, operation, endpoint, reason, tick):
        self.store.insert(
            "calls",
            run_id=run_id,
            ticket=ticket,
            scope=self._ticket_scope(ticket) or "",
            action=action,
            operation=operation,
            endpoint=endpoint,
            namespace=self.standins.namespace_of(endpoint) or "",
            status="rejected",
            reason=reason,
            tick=tick,
        )
        return {
            "accepted": False,
            "run": run_id,
            "ticket": ticket,
            "operation": operation,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # 轮次

    def start_run(self, run_id, team, snapshot="default", *, from_run=None,
                  same_run=False, at=None):
        """开启一轮班组演练，运行状态从指定的生产业务合成副本出发。"""
        tick = self._clock(at)
        self._start_run_row(run_id, team, snapshot, from_run, same_run, tick)
        return self.run_info(run_id)

    def _start_run_row(self, run_id, team, snapshot, from_run, same_run, tick):
        if snapshot not in self.snapshots:
            raise ValueError(f"未知起点：{snapshot}")
        if self._run(run_id) is not None:
            raise ValueError(f"轮次已存在：{run_id}")
        self.store.insert(
            "runs",
            run_id=run_id,
            team=team,
            snapshot=snapshot,
            status="active",
            started_tick=tick,
            from_run=from_run,
            same_run=int(same_run),
        )

    def run_info(self, run_id):
        run = self._run(run_id)
        if run is None:
            raise ValueError(f"未知轮次：{run_id}")
        info = dict(run)
        info["same_run"] = bool(info["same_run"])
        return info

    def list_runs(self):
        return [
            self.run_info(row["run_id"])
            for row in self.store.all("SELECT run_id FROM runs ORDER BY started_tick, run_id")
        ]

    # ------------------------------------------------------------------
    # 演练指令

    def call(self, run_id, ticket, action, operation, endpoint, *, at=None):
        """班组在一个轮次内发出可追踪的派单/确认/撤回/通知意图。

        指令只在该轮次内有效；送入演练端替身。错投到实际业务端替身的
        指令会被送达并留下可观测的错投记录。
        """
        tick = self._clock(at)
        run = self._run(run_id)
        if run is None or run["status"] != "active":
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     "轮次不存在或已结束", tick)
        scope = self._ticket_scope(ticket)
        if scope is None or scope not in {
            row["scope"] for row in self.snapshots[run["snapshot"]]
        }:
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     "工单不在演练范围", tick)
        if self._is_handed_over(scope):
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     "范围已移交实际业务", tick)
        incident = self._blocking_incident(scope)
        if incident is not None:
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     f"范围已被收回（{incident}）", tick)
        if self.store.one(
            "SELECT call_id FROM calls WHERE run_id=? AND operation=? AND status<>'rejected'",
            (run_id, operation),
        ):
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     "操作标识重复", tick)
        namespace = self.standins.namespace_of(endpoint)
        if namespace is None:
            return self._reject_call(run_id, ticket, action, operation, endpoint,
                                     "未知替身地址", tick)
        if namespace == "live":
            # 错投：指令进入实际业务替身并留下可观测记录，演练侧不可经回执收尾
            reason = "演练指令错投实际业务替身"
            self.store.insert(
                "calls",
                run_id=run_id, ticket=ticket, scope=scope, action=action,
                operation=operation, endpoint=endpoint, namespace=namespace,
                status="misdelivered", reason=reason, tick=tick,
            )
            self.standins.receive(endpoint, operation, ticket, action, run_id,
                                  misdelivered=True, tick=tick)
            return {
                "accepted": False,
                "misdelivered": True,
                "run": run_id,
                "ticket": ticket,
                "operation": operation,
                "endpoint": endpoint,
                "reason": reason,
            }
        if action == "notify":
            quota = self.quotas.get(scope)
            if quota is not None:
                inflight = self.store.one(
                    "SELECT COUNT(*) AS n FROM calls "
                    "WHERE run_id=? AND scope=? AND action='notify' AND status='pending'",
                    (run_id, scope),
                )["n"]
                if inflight >= quota:
                    return self._reject_call(run_id, ticket, action, operation,
                                             endpoint, "通知替身额度已用尽", tick)
        self.store.insert(
            "calls",
            run_id=run_id, ticket=ticket, scope=scope, action=action,
            operation=operation, endpoint=endpoint, namespace=namespace,
            status="pending", reason=None, tick=tick,
        )
        self.standins.receive(endpoint, operation, ticket, action, run_id,
                              misdelivered=False, tick=tick)
        return {
            "accepted": True,
            "run": run_id,
            "ticket": ticket,
            "scope": scope,
            "operation": operation,
            "endpoint": endpoint,
        }

    # ------------------------------------------------------------------
    # 回执

    def receive_receipt(self, run_id, ticket, operation, namespace, endpoint, state,
                        *, at=None):
        """登记一条替身回执并归账。

        演练回执按（轮次, 操作名）严格匹配，旧轮次的回执不会归给新一轮
        发出的同名请求；实际业务回执不提供轮次，一律不归入任何演练轮次，
        只作为接管核验的证据留存。
        """
        tick = self._clock(at)
        if namespace not in ("drill", "live"):
            raise ValueError("回执命名空间应为 drill 或 live")
        if namespace == "live":
            run_id = None
        matched_call_id = None
        flag = None
        if namespace == "drill":
            call = self.store.one(
                "SELECT * FROM calls WHERE run_id=? AND operation=? "
                "AND status<>'rejected' ORDER BY call_id DESC",
                (run_id, operation),
            )
            if call is None:
                flag = "orphan"
            elif call["status"] == "pending":
                matched_call_id = call["call_id"]
                self.store.execute(
                    "UPDATE calls SET status=? WHERE call_id=?",
                    (state if state in ("done", "cancelled") else state,
                     matched_call_id),
                )
                if self._crossed_interruption(call["scope"], call["tick"], tick):
                    flag = "after_revoke"
                self.standins.complete(endpoint, operation, run_id, state)
                if call["action"] == "handoff" and state == "done":
                    # 演练生成的交接材料仅供复盘，不能作为接管凭据
                    self.store.insert(
                        "handoff_packages",
                        run_id=run_id, ticket=ticket, operation=operation,
                        review_only=1, tick=tick,
                    )
            elif call["status"] == "misdelivered":
                matched_call_id = call["call_id"]
                flag = "misdelivered_call"
            else:
                matched_call_id = call["call_id"]
                flag = "duplicate"
        else:
            self.standins.complete(endpoint, operation, None, state)
        receipt_id = self.store.insert(
            "receipts",
            run_id=run_id, ticket=ticket, operation=operation, namespace=namespace,
            endpoint=endpoint, state=state, tick=tick,
            matched_call_id=matched_call_id, flag=flag,
        )
        return {
            "receipt_id": receipt_id,
            "namespace": namespace,
            "run": run_id,
            "operation": operation,
            "matched_call_id": matched_call_id,
            "flag": flag,
        }

    def settle(self, endpoint, operation, state, *, at=None):
        """替身收尾：让指定替身上待收尾的操作产生回执（查证或受控收尾）。"""
        namespace = self.standins.namespace_of(endpoint)
        if namespace is None:
            raise ValueError(f"未知替身地址：{endpoint}")
        op = self.store.one(
            "SELECT * FROM standin_ops WHERE endpoint=? AND operation=? "
            "AND status='received' ORDER BY op_id DESC",
            (endpoint, operation),
        )
        if op is None:
            raise ValueError(f"替身 {endpoint} 上没有待收尾的操作 {operation}")
        run_id = op["run_id"] if namespace == "drill" else None
        return self.receive_receipt(run_id, op["ticket"], operation, namespace,
                                    endpoint, state, at=at)

    def finalize_pending(self, scopes, state="cancelled", *, at=None):
        """把指定范围内仍压在演练端替身上的操作受控收尾。"""
        finished = []
        for op in self.standins.pending():
            if op["namespace"] != "drill" or op["misdelivered"]:
                continue
            if self._ticket_scope(op["ticket"]) in scopes:
                finished.append(
                    self.settle(op["endpoint"], op["operation"], state, at=at)
                )
        return finished

    # ------------------------------------------------------------------
    # 部分中断与接管

    def supersede(self, incident, supervisor, scopes, *, at=None):
        """真实告警插入：主管收回特定范围的人员与模拟操作权限。

        暂停只代表停止新动作；已经发给替身的动作列入在途清单，
        须通过回执查证或受控收尾。未受影响范围可继续演练。
        """
        tick = self._clock(at)
        if not scopes or not all(isinstance(scope, str) for scope in scopes):
            raise ValueError("收回范围应为非空字符串列表")
        known = set(self._scopes.values()) | set(self.quotas)
        unknown = [scope for scope in scopes if scope not in known]
        if unknown:
            raise ValueError(f"未知范围：{unknown}")
        interruption_id = self.store.insert(
            "interruptions",
            incident=incident, supervisor=supervisor,
            scopes=json.dumps(list(scopes), ensure_ascii=False),
            tick=tick, lifted_tick=None,
        )
        in_flight = [
            dict(row)
            for row in self.store.all(
                "SELECT * FROM calls WHERE status IN ('pending', 'misdelivered') "
                "ORDER BY tick, call_id"
            )
            if row["scope"] in scopes
        ]
        return {
            "interruption_id": interruption_id,
            "incident": incident,
            "scopes": list(scopes),
            "tick": tick,
            "in_flight": in_flight,
        }

    def handoff(self, incident, ticket, scope, operation, *, at=None):
        """办理实际业务接管。

        接管凭据必须来自实际业务端的证据（live 命名空间回执），并覆盖
        主管指定的范围；替身成功回执与演练生成的交接材料仅供复盘，
        不能作为凭据。
        """
        tick = self._clock(at)

        def reject(reason):
            self.store.insert(
                "handoffs",
                incident=incident, ticket=ticket, scope=scope, operation=operation,
                status="rejected", credential_receipt_id=None, reason=reason,
                tick=tick,
            )
            return {"accepted": False, "incident": incident, "scope": scope,
                    "operation": operation, "reason": reason}

        interruption = self.store.one(
            "SELECT * FROM interruptions WHERE incident=? ORDER BY tick DESC",
            (incident,),
        )
        if interruption is None:
            return reject("无对应中断")
        if interruption["lifted_tick"] is not None:
            return reject("中断已解除")
        if scope not in json.loads(interruption["scopes"]):
            return reject("范围不在主管收回清单内")
        if self._ticket_scope(ticket) != scope:
            return reject("工单不属于该范围")
        evidence = [
            row
            for row in self.store.all(
                "SELECT * FROM receipts WHERE namespace='live' AND operation=? "
                "AND ticket=? ORDER BY tick, receipt_id",
                (operation, ticket),
            )
            if row["state"] in _EVIDENCE_STATES
        ]
        if not evidence:
            package = self.store.one(
                "SELECT package_id FROM handoff_packages WHERE operation=? AND ticket=?",
                (operation, ticket),
            )
            if package is not None:
                return reject("演练交接材料仅供复盘，不能作为接管凭据")
            drill_only = self.store.one(
                "SELECT receipt_id FROM receipts WHERE namespace='drill' "
                "AND operation=? AND ticket=?",
                (operation, ticket),
            )
            if drill_only is not None:
                return reject("替身回执不能供实际接管核验")
            return reject("缺少实际业务端证据")
        credential = evidence[0]
        handoff_id = self.store.insert(
            "handoffs",
            incident=incident, ticket=ticket, scope=scope, operation=operation,
            status="accepted", credential_receipt_id=credential["receipt_id"],
            reason=None, tick=tick,
        )
        return {
            "accepted": True,
            "handoff_id": handoff_id,
            "incident": incident,
            "scope": scope,
            "operation": operation,
            "credential": {
                "receipt_id": credential["receipt_id"],
                "namespace": credential["namespace"],
                "endpoint": credential["endpoint"],
                "state": credential["state"],
                "tick": credential["tick"],
                "covers_scopes": [scope],
            },
        }

    # ------------------------------------------------------------------
    # 恢复与再开

    def resume(self, run_id, from_run, *, snapshot="default", same_run, team=None,
               at=None):
        """恢复被收回的范围：明确接续原轮次，或从已知起点再开一轮。

        恢复前报告上次中断以来已经改变的对象；旧轮次的回执不会归给
        新一轮发出的请求。
        """
        tick = self._clock(at)
        active = self.store.all("SELECT * FROM interruptions WHERE lifted_tick IS NULL")
        since = min((row["tick"] for row in active), default=tick)
        changed = self.changes_since(since)
        lifted = sorted({scope for row in active for scope in json.loads(row["scopes"])})
        if same_run:
            if run_id != from_run:
                raise ValueError("接续原演练必须使用原轮次标识")
            run = self._run(run_id)
            if run is None or run["status"] != "active":
                raise ValueError(f"原轮次不可接续：{run_id}")
        else:
            origin = self._run(from_run)
            if origin is None:
                raise ValueError(f"原轮次未知，无法作为接续依据：{from_run}")
            self._start_run_row(run_id, team or origin["team"], snapshot,
                                from_run, False, tick)
        for row in active:
            self.store.execute(
                "UPDATE interruptions SET lifted_tick=? WHERE interruption_id=?",
                (tick, row["interruption_id"]),
            )
        return {
            "run": run_id,
            "same_run": bool(same_run),
            "from_run": from_run,
            "lifted_scopes": lifted,
            "changed": changed,
            "tick": tick,
        }

    # ------------------------------------------------------------------
    # 观测与查询

    def changes_since(self, since_tick):
        """列出某时刻以来已经改变的对象：回执、新指令、错投、在途操作、工单状态。"""
        return {
            "since": since_tick,
            "receipts": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM receipts WHERE tick >= ? ORDER BY tick, receipt_id",
                    (since_tick,),
                )
            ],
            "new_calls": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM calls WHERE tick >= ? AND status<>'rejected' "
                    "ORDER BY tick, call_id",
                    (since_tick,),
                )
            ],
            "misdelivered": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM calls WHERE tick >= ? AND status='misdelivered' "
                    "ORDER BY tick, call_id",
                    (since_tick,),
                )
            ],
            "pending_stand_in_ops": self.standins.pending(),
            "ticket_changes": self._ticket_changes(),
        }

    def _ticket_changes(self):
        changes = {}
        for row in self.store.all("SELECT run_id FROM runs WHERE status='active'"):
            results = self.run_results(row["run_id"])
            diff = {
                ticket: {"from": info["initial"], "to": info["state"]}
                for ticket, info in results["tickets"].items()
                if info["initial"] != info["state"]
            }
            if diff:
                changes[row["run_id"]] = diff
        return changes

    def run_results(self, run_id):
        """一个轮次内各工单的实际处置结果（与其他班组互不计入）。"""
        run = self._run(run_id)
        if run is None:
            raise ValueError(f"未知轮次：{run_id}")
        tickets = {}
        for row in self.snapshots[run["snapshot"]]:
            tickets[row["ticket"]] = {
                "scope": row["scope"],
                "initial": row["state"],
                "state": row["state"],
                "notified": False,
                "actions": [],
                "pending": 0,
            }
        for call in self.store.all(
            "SELECT * FROM calls WHERE run_id=? AND status<>'rejected' "
            "ORDER BY tick, call_id",
            (run_id,),
        ):
            info = tickets.get(call["ticket"])
            if info is None:
                continue
            info["actions"].append({
                "action": call["action"],
                "operation": call["operation"],
                "status": call["status"],
                "endpoint": call["endpoint"],
                "namespace": call["namespace"],
            })
            if call["status"] == "pending":
                info["pending"] += 1
            elif call["status"] == "done":
                if call["action"] == "notify":
                    info["notified"] = True
                else:
                    info["state"] = _TICKET_TRANSITIONS.get(
                        call["action"], info["state"]
                    )
        all_handled = bool(tickets) and all(
            info["pending"] == 0
            and any(action["status"] == "done" for action in info["actions"])
            for info in tickets.values()
        )
        return {
            "run": run_id,
            "team": run["team"],
            "snapshot": run["snapshot"],
            "tickets": tickets,
            "all_handled": all_handled,
        }

    def compare_runs(self, run_a, run_b):
        """对照两个班组在同一场景下的过程偏离。

        各班结果按各自轮次独立判定，另一组先完成的处置不会计入这一组。
        """
        import difflib

        results_a = self.run_results(run_a)
        results_b = self.run_results(run_b)
        deviations = []
        state_diffs = {}
        tickets = sorted(set(results_a["tickets"]) | set(results_b["tickets"]))
        for ticket in tickets:
            steps_a = [
                (a["action"], a["status"])
                for a in results_a["tickets"].get(ticket, {}).get("actions", [])
            ]
            steps_b = [
                (a["action"], a["status"])
                for a in results_b["tickets"].get(ticket, {}).get("actions", [])
            ]
            if steps_a != steps_b:
                matcher = difflib.SequenceMatcher(
                    None, [s[0] for s in steps_a], [s[0] for s in steps_b]
                )
                diff = [
                    {
                        "op": tag,
                        run_a: steps_a[i1:i2],
                        run_b: steps_b[j1:j2],
                    }
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                    if tag != "equal"
                ]
                deviations.append({
                    "ticket": ticket,
                    run_a: steps_a,
                    run_b: steps_b,
                    "diff": diff,
                })
            state_a = results_a["tickets"].get(ticket, {}).get("state")
            state_b = results_b["tickets"].get(ticket, {}).get("state")
            if state_a != state_b:
                state_diffs[ticket] = {run_a: state_a, run_b: state_b}
        return {
            "runs": [run_a, run_b],
            "teams": [results_a["team"], results_b["team"]],
            "deviations": deviations,
            "state_diffs": state_diffs,
            "isolated": True,
        }

    def standin_inbox(self, endpoint):
        """查看某个替身地址收到的全部操作（含错投标记）。"""
        if self.standins.namespace_of(endpoint) is None:
            raise ValueError(f"未知替身地址：{endpoint}")
        return self.standins.inbox(endpoint)

    def standin_overview(self):
        """两套替身各自收到的操作，按地址分组。"""
        return {
            namespace: {
                endpoint: self.standins.inbox(endpoint)
                for endpoint in self.standins.endpoints(namespace)
            }
            for namespace in ("drill", "live")
        }

    def violations(self):
        """可观测的异常：错投、被拒指令、异常回执、被拒接管。"""
        return {
            "misdelivered_calls": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM calls WHERE status='misdelivered' ORDER BY tick"
                )
            ],
            "rejected_calls": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM calls WHERE status='rejected' ORDER BY tick"
                )
            ],
            "flagged_receipts": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM receipts WHERE flag IS NOT NULL ORDER BY tick"
                )
            ],
            "rejected_handoffs": [
                dict(row)
                for row in self.store.all(
                    "SELECT * FROM handoffs WHERE status='rejected' ORDER BY tick"
                )
            ],
        }

    def interruptions(self):
        """全部中断（被收回的范围）及其解除情况。"""
        return [
            {
                **dict(row),
                "scopes": json.loads(row["scopes"]),
                "active": row["lifted_tick"] is None,
            }
            for row in self.store.all(
                "SELECT * FROM interruptions ORDER BY tick, interruption_id"
            )
        ]

    def takeovers(self):
        """全部接管办理记录；被接受的接管附带实际业务端凭据。"""
        items = []
        for row in self.store.all("SELECT * FROM handoffs ORDER BY tick, handoff_id"):
            item = dict(row)
            if row["credential_receipt_id"] is not None:
                item["credential"] = dict(
                    self.store.one(
                        "SELECT * FROM receipts WHERE receipt_id=?",
                        (row["credential_receipt_id"],),
                    )
                )
            items.append(item)
        return items

    def review_materials(self, run_id=None):
        """演练生成的交接材料（仅供复盘，不能作为接管凭据）。"""
        if run_id is None:
            rows = self.store.all(
                "SELECT * FROM handoff_packages ORDER BY tick, package_id"
            )
        else:
            rows = self.store.all(
                "SELECT * FROM handoff_packages WHERE run_id=? "
                "ORDER BY tick, package_id",
                (run_id,),
            )
        return [dict(row) for row in rows]
