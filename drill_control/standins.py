"""本地两套可区分的替身：演练端（drill）与实际业务端（live）。

通知、工单、接管三类替身在两套命名空间下各有独立地址。替身只负责
收件、留存与收尾：收到的每个操作都落库（含错投标记），可以按地址
查看各自收到了什么，错投到实际业务端的演练指令由此可观测。
"""


class StandInNetwork:
    """按清册 endpoints 建立的替身网络。"""

    def __init__(self, store, endpoints):
        self.store = store
        self._groups = {ns: list(eps) for ns, eps in endpoints.items()}
        self._namespace = {ep: ns for ns, eps in self._groups.items() for ep in eps}

    def namespace_of(self, endpoint):
        """返回替身地址所属命名空间（drill/live），未知地址返回 None。"""
        return self._namespace.get(endpoint)

    def endpoints(self, namespace=None):
        if namespace is None:
            return [ep for ns in sorted(self._groups) for ep in self._groups[ns]]
        return list(self._groups.get(namespace, []))

    def receive(self, endpoint, operation, ticket, action, run_id, misdelivered, tick):
        """替身在指定地址收到一个操作。错投的演练指令带 misdelivered 标记。"""
        return self.store.insert(
            "standin_ops",
            endpoint=endpoint,
            namespace=self._namespace[endpoint],
            operation=operation,
            ticket=ticket,
            action=action,
            run_id=run_id,
            misdelivered=int(misdelivered),
            status="received",
            tick=tick,
        )

    def complete(self, endpoint, operation, run_id, state):
        """回执到达时把替身上对应操作标记为已收尾。

        实际业务回执不提供轮次（run_id 为 None），只按地址与操作名归账，
        因此错投到实际业务端的演练指令无法经演练回执收尾。
        """
        if run_id is None:
            self.store.execute(
                "UPDATE standin_ops SET status=? "
                "WHERE endpoint=? AND operation=? AND status='received'",
                (state, endpoint, operation),
            )
        else:
            self.store.execute(
                "UPDATE standin_ops SET status=? "
                "WHERE endpoint=? AND operation=? AND run_id=? AND status='received'",
                (state, endpoint, operation, run_id),
            )

    def inbox(self, endpoint):
        """某个替身地址收到的全部操作。"""
        return [
            dict(row)
            for row in self.store.all(
                "SELECT * FROM standin_ops WHERE endpoint=? ORDER BY tick, op_id",
                (endpoint,),
            )
        ]

    def pending(self):
        """仍压在替身上、尚未收尾的操作。"""
        return [
            dict(row)
            for row in self.store.all(
                "SELECT * FROM standin_ops WHERE status='received' ORDER BY tick, op_id"
            )
        ]
