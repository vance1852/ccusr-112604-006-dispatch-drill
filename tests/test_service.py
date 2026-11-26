"""演练控制服务的行为核验：隔离、资源、中断、接管、恢复与持久化。"""

import json
import tempfile
import unittest
from pathlib import Path

from drill_control import DrillControlService
from drill_control.replay import load_inventory, load_timeline, replay

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "samples" / "inventory.json").read_text(encoding="utf-8"))


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "drill.db")
        self.service = DrillControlService(self.db, INVENTORY)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def drill_receipt(self, run, ticket, operation, endpoint="desk-drill", state="done"):
        return self.service.receive_receipt(run, ticket, operation, "drill", endpoint, state)


class TeamIsolationTests(ServiceTestCase):
    def test_two_teams_share_scenario_without_cross_credit(self):
        self.service.start_run("north-1", "north")
        self.service.start_run("south-1", "south")
        self.service.call("north-1", "H-18", "assign", "n-assign", "desk-drill")
        self.drill_receipt("north-1", "H-18", "n-assign")

        # 另一组未完成的处置不计入这一组：south-1 的 H-18 仍是初始状态
        south = self.service.run_results("south-1")
        self.assertEqual(south["tickets"]["H-18"]["state"], "open")
        self.assertFalse(south["all_handled"])

        # south-1 仍可在自己的轮次内处置同一工单
        outcome = self.service.call("south-1", "H-18", "assign", "s-assign", "desk-drill")
        self.assertTrue(outcome["accepted"])

        compared = self.service.compare_runs("north-1", "south-1")
        self.assertTrue(compared["isolated"])
        self.assertEqual(compared["state_diffs"]["H-18"],
                         {"north-1": "assigned", "south-1": "open"})
        self.assertTrue(any(d["ticket"] == "H-18" for d in compared["deviations"]))

    def test_same_business_ticket_keeps_drill_and_live_identities(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "assign", "rep18", "desk-drill")
        self.drill_receipt("north-1", "H-18", "rep18")
        # 同一业务单在实际业务侧的回执不归入演练轮次，也不算重复
        live = self.service.receive_receipt(None, "H-18", "rep18", "live",
                                            "lead-real", "accepted")
        self.assertIsNone(live["run"])
        self.assertIsNone(live["flag"])
        self.assertIsNone(live["matched_call_id"])
        north = self.service.run_results("north-1")
        self.assertEqual(len(north["tickets"]["H-18"]["actions"]), 1)


class ResourceTests(ServiceTestCase):
    def test_notice_quota_is_consumable_per_run(self):
        self.service.start_run("north-1", "north")
        first = self.service.call("north-1", "H-18", "notify", "note1", "notice-drill")
        second = self.service.call("north-1", "H-18", "notify", "note2", "notice-drill")
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["reason"], "通知替身额度已用尽")
        # 回执释放额度后可再发
        self.drill_receipt("north-1", "H-18", "note1", endpoint="notice-drill")
        third = self.service.call("north-1", "H-18", "notify", "note3", "notice-drill")
        self.assertTrue(third["accepted"])

    def test_notice_quota_is_per_scope(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-19", "notify", "b1", "notice-drill")
        accepted = self.service.call("north-1", "H-19", "notify", "b2", "notice-drill")
        self.assertTrue(accepted["accepted"])  # room-b 额度为 2

    def test_unknown_endpoint_and_duplicate_operation_rejected(self):
        self.service.start_run("north-1", "north")
        bad = self.service.call("north-1", "H-18", "assign", "x1", "nowhere")
        self.assertEqual(bad["reason"], "未知替身地址")
        self.service.call("north-1", "H-18", "assign", "x2", "desk-drill")
        dup = self.service.call("north-1", "H-18", "confirm", "x2", "desk-drill")
        self.assertEqual(dup["reason"], "操作标识重复")


class MisdeliveryTests(ServiceTestCase):
    def test_misdelivery_is_observable_on_live_standin(self):
        self.service.start_run("shift-c", "central")
        outcome = self.service.call("shift-c", "H-18", "assign", "r2", "notice-real")
        self.assertFalse(outcome["accepted"])
        self.assertTrue(outcome["misdelivered"])

        inbox = self.service.standin_inbox("notice-real")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["misdelivered"], 1)
        self.assertEqual(self.service.standin_inbox("notice-drill"), [])
        violations = self.service.violations()
        self.assertEqual([c["operation"] for c in violations["misdelivered_calls"]],
                         ["r2"])

        # 错投指令无法经演练回执收尾；实际业务回执只更新替身收件箱
        live = self.service.receive_receipt(None, "H-18", "r2", "live",
                                            "notice-real", "done")
        self.assertIsNone(live["matched_call_id"])
        results = self.service.run_results("shift-c")
        self.assertEqual(results["tickets"]["H-18"]["state"], "open")
        self.assertEqual(self.service.standin_inbox("notice-real")[0]["status"], "done")


class InterruptionTests(ServiceTestCase):
    def test_partial_interruption_scoped_and_in_flight_tracked(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "assign", "a1", "desk-drill")
        report = self.service.supersede("live5", "team-lead", ["room-a"])
        self.assertEqual([c["operation"] for c in report["in_flight"]], ["a1"])

        # 被收回范围停止新动作，未受影响范围仍可继续
        blocked = self.service.call("north-1", "H-18", "confirm", "a2", "desk-drill")
        self.assertIn("范围已被收回", blocked["reason"])
        allowed = self.service.call("north-1", "H-19", "cancel", "b1", "desk-drill")
        self.assertTrue(allowed["accepted"])

        # 已发给替身的动作可查证：收回后完成的回执带标记
        receipt = self.drill_receipt("north-1", "H-18", "a1")
        self.assertEqual(receipt["flag"], "after_revoke")

    def test_controlled_wrapup_of_pending_ops(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "notify", "n1", "notice-drill")
        self.service.supersede("live5", "team-lead", ["room-a"])
        finished = self.service.finalize_pending(["room-a"])
        self.assertEqual(len(finished), 1)
        results = self.service.run_results("north-1")
        self.assertEqual(results["tickets"]["H-18"]["pending"], 0)
        self.assertEqual(self.service.standins.pending(), [])

    def test_unknown_scope_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知范围"):
            self.service.supersede("live5", "team-lead", ["room-z"])


class HandoffTests(ServiceTestCase):
    def test_takeover_requires_live_evidence_covering_scope(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "assign", "rep18", "desk-drill")
        self.service.supersede("live5", "team-lead", ["room-a"])
        self.drill_receipt("north-1", "H-18", "rep18")

        # 替身成功回执不能供实际接管核验
        rejected = self.service.handoff("live5", "H-18", "room-a", "rep18")
        self.assertFalse(rejected["accepted"])
        self.assertIn("替身回执", rejected["reason"])

        # 实际业务端证据到达后接管成立，凭据覆盖主管指定范围
        self.service.receive_receipt(None, "H-18", "rep18", "live",
                                     "lead-real", "accepted")
        accepted = self.service.handoff("live5", "H-18", "room-a", "rep18")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["credential"]["namespace"], "live")
        self.assertEqual(accepted["credential"]["covers_scopes"], ["room-a"])

        # 已接管范围退出演练
        blocked = self.service.call("north-1", "H-18", "confirm", "c1", "desk-drill")
        self.assertEqual(blocked["reason"], "范围已移交实际业务")

    def test_handoff_without_any_evidence_rejected(self):
        self.service.start_run("shift-c", "central")
        self.service.supersede("alert2", "team-lead", ["room-a"])
        outcome = self.service.handoff("alert2", "H-18", "room-a", "g7")
        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "缺少实际业务端证据")

    def test_handoff_scope_must_match_supervisor_list(self):
        self.service.start_run("north-1", "north")
        self.service.supersede("live5", "team-lead", ["room-a"])
        outcome = self.service.handoff("live5", "H-19", "room-b", "op9")
        self.assertEqual(outcome["reason"], "范围不在主管收回清单内")

    def test_drill_handoff_package_is_review_only(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "handoff", "pkg1", "lead-drill")
        self.drill_receipt("north-1", "H-18", "pkg1", endpoint="lead-drill")
        materials = self.service.review_materials("north-1")
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0]["review_only"], 1)

        self.service.supersede("live5", "team-lead", ["room-a"])
        outcome = self.service.handoff("live5", "H-18", "room-a", "pkg1")
        self.assertFalse(outcome["accepted"])
        self.assertIn("仅供复盘", outcome["reason"])


class ResumeTests(ServiceTestCase):
    def test_resume_same_run_reports_changes_and_reopens_scope(self):
        self.service.start_run("shift-c", "central")
        self.service.call("shift-c", "H-18", "assign", "r1", "notice-drill")
        self.service.supersede("alert2", "team-lead", ["room-a"])
        self.drill_receipt("shift-c", "H-18", "r1", endpoint="notice-drill",
                           state="cancelled")

        resumed = self.service.resume("shift-c", "shift-c", same_run=True)
        self.assertEqual(resumed["lifted_scopes"], ["room-a"])
        changed_states = [r["state"] for r in resumed["changed"]["receipts"]]
        self.assertIn("cancelled", changed_states)

        again = self.service.call("shift-c", "H-18", "assign", "r9", "desk-drill")
        self.assertTrue(again["accepted"])

    def test_resume_new_run_does_not_inherit_old_receipts(self):
        self.service.start_run("north-1", "north")
        self.service.call("north-1", "H-18", "assign", "rep18", "desk-drill")
        self.service.supersede("live5", "team-lead", ["room-a"])
        resumed = self.service.resume("north-2", "north-1", same_run=False)
        self.assertEqual(resumed["from_run"], "north-1")

        # 新一轮从已知起点再开：工单状态不继承旧轮次
        fresh = self.service.run_results("north-2")
        self.assertEqual(fresh["tickets"]["H-18"]["state"], "open")
        # 新一轮可复用同名操作；旧轮次的迟到回执不会归给新一轮
        self.service.call("north-2", "H-18", "assign", "rep18", "desk-drill")
        late = self.drill_receipt("north-1", "H-18", "rep18")
        self.assertEqual(late["flag"], "after_revoke")
        self.assertEqual(self.service.run_results("north-2")
                         ["tickets"]["H-18"]["pending"], 1)
        own = self.drill_receipt("north-2", "H-18", "rep18")
        self.assertIsNone(own["flag"])
        self.assertEqual(self.service.run_results("north-2")
                         ["tickets"]["H-18"]["state"], "assigned")

    def test_resume_requires_explicit_mode(self):
        self.service.start_run("north-1", "north")
        with self.assertRaisesRegex(ValueError, "原轮次标识"):
            self.service.resume("north-2", "north-1", same_run=True)
        with self.assertRaisesRegex(ValueError, "原轮次未知"):
            self.service.resume("north-2", "ghost", same_run=False)


class PersistenceTests(ServiceTestCase):
    def test_restart_preserves_rounds_interruptions_results_credentials(self):
        records = load_timeline(ROOT / "samples" / "shift-log.jsonl")
        replay(self.service, records)
        self.service.close()

        reopened = DrillControlService(self.db)  # 重启后无需再提供清册
        self.addCleanup(reopened.close)
        self.assertEqual([r["run_id"] for r in reopened.list_runs()],
                         ["north-1", "south-1", "north-2"])
        interruptions = reopened.interruptions()
        self.assertEqual(interruptions[0]["scopes"], ["room-a"])
        self.assertFalse(interruptions[0]["active"])
        takeovers = reopened.takeovers()
        self.assertEqual(takeovers[0]["status"], "accepted")
        self.assertEqual(takeovers[0]["credential"]["endpoint"], "lead-real")
        north = reopened.run_results("north-1")
        self.assertEqual(north["tickets"]["H-19"]["state"], "cancelled")

    def test_inventory_mismatch_rejected(self):
        self.service.close()
        other = dict(INVENTORY, snapshot=[{"ticket": "X", "scope": "room-a",
                                           "state": "open"}])
        with self.assertRaisesRegex(ValueError, "不一致"):
            DrillControlService(self.db, other)


class ReplayTests(ServiceTestCase):
    def replay_sample(self, name):
        return replay(self.service, load_timeline(ROOT / "samples" / name))

    def test_shift_log_expectations(self):
        outcomes = self.replay_sample("shift-log.jsonl")
        by_op = {}
        for step in outcomes:
            if step["kind"] == "call":
                by_op[step["record"]["operation"]] = step["outcome"]
        # room-a 同时通知额度为 1：第二条通知被拒
        self.assertFalse(by_op["note18b"]["accepted"])
        self.assertEqual(by_op["note18b"]["reason"], "通知替身额度已用尽")

        handoff = [s["outcome"] for s in outcomes if s["kind"] == "handoff"][0]
        self.assertTrue(handoff["accepted"])
        self.assertEqual(handoff["credential"]["endpoint"], "lead-real")

        # 收回后完成的回执可查证
        flagged = {r["operation"] for r in self.service.violations()["flagged_receipts"]}
        self.assertEqual(flagged, {"rep18"})

        # 两班各自推进，偏差可对照
        compared = self.service.compare_runs("north-1", "south-1")
        self.assertTrue(compared["deviations"])

    def test_partial_resume_expectations(self):
        outcomes = self.replay_sample("partial-resume.jsonl")
        calls = {s["record"]["operation"]: s["outcome"]
                 for s in outcomes if s["kind"] == "call"}
        # 错投到实际业务替身可观测
        self.assertTrue(calls["r2"]["misdelivered"])
        inbox = self.service.standin_inbox("notice-real")
        self.assertEqual([op["operation"] for op in inbox], ["r2"])
        self.assertEqual(inbox[0]["misdelivered"], 1)

        # 无实际业务端证据的接管被拒
        handoff = [s["outcome"] for s in outcomes if s["kind"] == "handoff"][0]
        self.assertFalse(handoff["accepted"])
        self.assertEqual(handoff["reason"], "缺少实际业务端证据")

        # 接续原轮次恢复，被收回范围重新开放
        resumed = [s["outcome"] for s in outcomes if s["kind"] == "resume"][0]
        self.assertTrue(resumed["same_run"])
        self.assertEqual(resumed["lifted_scopes"], ["room-a"])
        self.assertFalse(self.service.interruptions()[0]["active"])


if __name__ == "__main__":
    unittest.main()
