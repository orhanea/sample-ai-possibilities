"""Protocol and tactical invariants; all tests run without AWS or Strands."""

import asyncio
from concurrent.futures import Future
import copy
import importlib.util
import json
import logging
from pathlib import Path
import random
import runpy
import sys
import tempfile
from threading import Event
from time import perf_counter
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "lib"))

from total_football.commands import fallback, validate
from total_football.memory import OpponentMemory
from total_football.prompts import context_text
from total_football.runtime import DecisionEngine, InferenceBusy, SingleFlightModel, parse_one
from total_football.tactics import ROLES, build_plan, pass_options, shot_quality


def snapshot():
    players = []
    for team in (0, 1):
        for pid, (x, y) in enumerate(((-50, 0), (-25, 0), (-5, -17), (12, 15), (30, -4))):
            players.append({"agentId": f"agentId_{pid}", "teamCode": "HOME" if team == 0 else "away",
                            "position": {"x": x * (1 if team == 0 else -1), "y": y},
                            "velocity": {"x": 0, "y": 0}, "stamina": 0.8})
    return {"tick": 20, "gameTime": 40, "matchDuration": 600, "matchId": "match-a",
            "score": {"home": 0, "away": 0}, "playMode": "OPEN_PLAY", "players": players,
            "ball": {"position": {"x": 12, "y": 15}, "isFree": False,
                     "possessionAgentId": "agentId_3", "possessionTeamId": 0}}


def action(kind, **params):
    return {"commandType": kind, "parameters": params}


class TacticalTests(unittest.TestCase):
    def test_unique_roles_and_permanent_goalkeeper(self):
        plan = build_plan(snapshot(), 0)
        self.assertEqual(set(plan.roles.values()), {*ROLES, "SWEEPER_KEEPER"})
        self.assertEqual(plan.roles[0], "SWEEPER_KEEPER")
        self.assertLess(plan.targets[0][0], -43)

    def test_malformed_agent_id_is_not_a_phantom_keeper(self):
        state = snapshot()
        state["players"][0]["agentId"] = "broken"
        plan = build_plan(state, 0)
        self.assertNotIn(0, plan.roles)

    def test_missing_outfielder_still_has_unique_available_roles(self):
        state = snapshot()
        state["players"] = [p for p in state["players"] if p["agentId"] != "agentId_4"]
        plan = build_plan(state, 0)
        self.assertEqual(len(plan.roles), 4)
        self.assertEqual(len(set(plan.roles.values())), 4)

    def test_roles_follow_position_instead_of_fixed_player_number(self):
        state = snapshot()
        state["players"][1]["position"], state["players"][4]["position"] = (
            state["players"][4]["position"], state["players"][1]["position"])
        plan = build_plan(state, 0)
        self.assertEqual(plan.roles[4], "ANCHOR")
        self.assertEqual(plan.roles[1], "SHADOW_STRIKER")

    def test_exhausted_attacker_rotates_into_lower_workload(self):
        state = snapshot()
        state["players"][1]["position"] = {"x": 0, "y": 0}
        state["players"][4]["position"] = {"x": 0, "y": 0}
        state["players"][4]["stamina"] = 0.2
        self.assertEqual(build_plan(state, 0).roles[4], "ANCHOR")

    def test_reordered_snapshot_produces_identical_coordination(self):
        state = snapshot()
        original = build_plan(state, 0)
        random.Random(4).shuffle(state["players"])
        reordered = build_plan(state, 0)
        self.assertEqual(original.roles, reordered.roles)
        self.assertEqual(original.targets, reordered.targets)
        self.assertEqual(original.presser, reordered.presser)

    def test_ambiguous_possession_never_claims_ownership(self):
        state = snapshot()
        del state["ball"]["possessionTeamId"]
        for team in (0, 1):
            plan = build_plan(state, team)
            self.assertEqual(plan.possession, "unknown")
            self.assertNotIn(fallback(plan, 3)["commandType"], ("PASS", "SHOOT", "GK_DISTRIBUTE"))

    def test_free_ball_overrides_stale_holder(self):
        state = snapshot()
        state["ball"]["isFree"] = True
        self.assertEqual(build_plan(state, 0).possession, "free")

    def test_legacy_payload_and_percent_stamina(self):
        state = snapshot()
        for i, p in enumerate(state["players"]):
            p["playerId"] = i % 5
            p["teamId"] = 0 if i < 5 else 1
            del p["agentId"], p["teamCode"]
            p["stamina"] *= 100
        del state["ball"]["possessionAgentId"]
        state["ball"]["possessionPlayerId"] = 3
        plan = build_plan(state, 0)
        self.assertEqual(plan.possession, "ours")
        self.assertEqual(plan.players[3].stamina, 0.8)

    def test_stamina_scale_override_handles_one_percent(self):
        state = snapshot()
        state["staminaScale"] = "percent"
        for p in state["players"]:
            p["stamina"] = 1
        self.assertEqual(build_plan(state, 0).players[0].stamina, 0.01)

    def test_single_presser_and_stamina_tiebreak(self):
        state = snapshot()
        state["ball"]["possessionTeamId"] = 1
        state["ball"]["position"] = {"x": 0, "y": 0}
        # Opponent holder is at the ball, with two equally close defenders.
        state["players"][8]["position"] = {"x": 0, "y": 0}
        for pid in (1, 2):
            state["players"][pid]["position"] = {"x": 2, "y": 0}
        state["players"][1]["stamina"] = 0.4
        state["players"][2]["stamina"] = 0.9
        plan = build_plan(state, 0)
        self.assertEqual(plan.presser, 2)
        self.assertEqual(sum(fallback(plan, p.pid)["commandType"] == "PRESS_BALL" for p in plan.players), 1)

    def test_score_and_remaining_clock_drive_formations(self):
        state = snapshot()
        state["gameTime"] = 550
        state["score"]["home"] = 1
        plan = build_plan(state, 0)
        self.assertEqual(plan.mode, "defensive")
        self.assertEqual(sum(x < 0 for x, _ in plan.targets.values()), 4)
        state["score"] = {"home": 0, "away": 1}
        plan = build_plan(state, 0)
        self.assertEqual(plan.mode, "attacking")
        self.assertEqual(sum(x > 0 for x, _ in plan.targets.values()), 3)

    def test_mirrored_pitch_has_mirrored_plan(self):
        state = snapshot()
        original = build_plan(state, 0)
        mirrored = copy.deepcopy(state)
        for p in mirrored["players"]:
            p["teamCode"] = "away" if p["teamCode"] == "HOME" else "home"
            p["position"]["x"] *= -1
        mirrored["ball"]["position"]["x"] *= -1
        mirrored["ball"]["possessionTeamId"] = 1
        away = build_plan(mirrored, 1)
        self.assertEqual(original.roles, away.roles)
        for pid, (x, y) in original.targets.items():
            self.assertEqual(away.targets[pid], (-x, y))

    def test_restart_shape_has_no_press_and_no_advanced_outfielders(self):
        state = snapshot()
        state["playMode"] = "KICK_OFF"
        plan = build_plan(state, 0)
        self.assertIsNone(plan.presser)
        self.assertTrue(all(x <= 0 for x, _ in plan.targets.values()))

    def test_pass_prefers_open_lane_to_blocked_forward(self):
        state = snapshot()
        state["players"][3]["position"] = {"x": 0, "y": 0}
        state["players"][4]["position"] = {"x": 20, "y": 0}
        state["players"][6]["position"] = {"x": 10, "y": 0}
        plan = build_plan(state, 0)
        options = pass_options(plan, plan.players[3])
        self.assertNotEqual(options[0]["player"], 4)

    def test_clear_close_shot_and_wide_angle(self):
        state = snapshot()
        state["players"][3]["position"] = {"x": 45, "y": 0}
        for p in state["players"][5:]:
            p["position"] = {"x": -20, "y": 30}
        plan = build_plan(state, 0)
        self.assertGreater(shot_quality(plan, plan.players[3]), 0.4)
        self.assertEqual(fallback(plan, 3)["commandType"], "SHOOT")
        state["players"][3]["position"]["y"] = 32
        wide = build_plan(state, 0)
        self.assertLess(shot_quality(wide, wide.players[3]), 0.4)

    def test_context_has_each_players_role_and_future_shape(self):
        state = snapshot()
        packet = json.loads(context_text(state, build_plan(state, 0), 3))
        self.assertEqual(len(packet["teamPlan"]), 5)
        self.assertIn("onLoss", packet["nextShape"])
        self.assertEqual(packet["identity"], "Maestro")

    def test_randomized_fallback_protocol_invariants(self):
        rng = random.Random(123)
        for case in range(250):
            state = snapshot()
            for p in state["players"]:
                p["position"] = {"x": rng.uniform(-54, 54), "y": rng.uniform(-34, 34)}
                p["stamina"] = rng.random()
            state["ball"]["position"] = {"x": rng.uniform(-54, 54), "y": rng.uniform(-34, 34)}
            state["ball"]["isFree"] = case % 3 == 0
            state["ball"]["possessionTeamId"] = case % 2
            for team in (0, 1):
                plan = build_plan(state, team)
                self.assertEqual(len(set(plan.roles.values())), 5)
                for pid in range(5):
                    cmd = fallback(plan, pid)
                    self.assertIsNotNone(validate(cmd, plan, pid), (case, team, pid, cmd))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.state = snapshot()
        self.plan = build_plan(self.state, 0)

    def test_rejects_missing_nan_and_boolean_coordinates(self):
        for value in (None, float("nan"), float("inf"), True, "2"):
            self.assertIsNone(validate(action("MOVE_TO", target_x=value, target_y=0, sprint=False), self.plan, 3))

    def test_ownership_targets_and_team_reset(self):
        for cmd, pid in ((action("PASS", target_player_id=3, type="GROUND"), 3),
                         (action("PASS", target_player_id=4, type="GROUND"), 2),
                         (action("GK_DISTRIBUTE", target_player_id=4, method="THROW"), 3),
                         (action("PASS", target_player_id=9, type="GROUND"), 3),
                         (action("RESET"), 3)):
            self.assertIsNone(validate(cmd, self.plan, pid))

    def test_repairs_attribution_and_clamps_movement(self):
        candidate = action("MOVE_TO", target_x=999, target_y=-999, sprint=False)
        candidate.update(playerId=4, teamId=1)
        result = validate(candidate, self.plan, 3)
        self.assertEqual((result["playerId"], result["teamId"]), (3, 0))
        self.assertEqual(result["parameters"]["target_x"], 53)
        self.assertEqual(result["parameters"]["target_y"], -33)

    def test_exhausted_player_cannot_sprint_except_emergency(self):
        self.state["players"][3]["stamina"] = 0.1
        plan = build_plan(self.state, 0)
        cmd = action("MOVE_TO", target_x=35, target_y=0, sprint=True)
        self.assertFalse(validate(cmd, plan, 3)["parameters"]["sprint"])
        self.state["ball"].update(possessionTeamId=1, position={"x": -45, "y": 0})
        self.state["players"][8]["position"] = {"x": -45, "y": 0}
        plan = build_plan(self.state, 0)
        self.assertTrue(validate(cmd, plan, 3)["parameters"]["sprint"])

    def test_rejects_multiple_commands_and_recovers_fenced_json(self):
        cmd = action("SET_STANCE", stance=0)
        self.assertIsNone(parse_one(json.dumps([cmd, cmd])))
        self.assertEqual(parse_one("```json\n" + json.dumps([cmd]) + "\n```"), cmd)


class MemoryTests(unittest.TestCase):
    def test_bounded_deduplicated_and_match_isolated(self):
        memory = OpponentMemory(capacity=4, max_matches=2)
        state = snapshot()
        for tick in range(10):
            state["tick"] = tick
            memory.observe("one", state, build_plan(state, 0))
        plan = build_plan(state, 0)
        memory.observe("one", state, plan)
        self.assertEqual(len(memory._matches[("one", 0)]), 4)
        self.assertEqual(len(memory.observe("two", state, plan)), 1)
        self.assertEqual(memory.observe(None, state, plan), ())
        memory.observe("three", state, plan)
        self.assertEqual(len(memory._matches), 2)
        self.assertNotIn(("one", 0), memory._matches)

    def test_late_snapshot_does_not_rewind_memory(self):
        memory = OpponentMemory()
        state = snapshot()
        plan = build_plan(state, 0)
        memory.observe("one", state, plan)
        state["tick"] = 1
        result = memory.observe("one", state, plan)
        self.assertEqual(result[-1]["tick"], 20)


class FakeModel:
    def __init__(self, response=None, error=None, pending=False):
        self.response, self.error, self.pending = response, error, pending
        self.future = None

    def start(self, text):
        self.future = Future()
        if self.error:
            self.future.set_exception(self.error)
        elif not self.pending:
            self.future.set_result(self.response)
        return self.future


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_llm_action_for_effective_player_and_away_team(self):
        response = json.dumps([action("MOVE_TO", target_x=10, target_y=4, sprint=False)])
        engine = DecisionEngine(3, model=FakeModel(response))
        commands = await engine.decide({"prompt": {"teamId": 1, "myPlayers": [2], "gameState": snapshot()}})
        self.assertEqual(commands[0]["playerId"], 2)
        self.assertEqual(commands[0]["teamId"], 1)

    async def test_invalid_or_error_model_uses_same_role_fallback(self):
        state = snapshot()
        expected = fallback(build_plan(state, 0), 3)
        for model in (FakeModel("garbage"), FakeModel(error=RuntimeError("unavailable"))):
            with self.assertLogs("total_football.runtime", level="WARNING"):
                result = await DecisionEngine(3, model=model).decide({"teamId": 0, "gameState": state})
            self.assertEqual(result, [expected])

    async def test_timeout_returns_fresh_fallback_and_discards_late_output(self):
        model = FakeModel(pending=True)
        engine = DecisionEngine(3, model=model, budget_seconds=0.025)
        start = perf_counter()
        result = await engine.decide({"teamId": 0, "gameState": snapshot()})
        self.assertLess(perf_counter() - start, 0.3)
        self.assertEqual(result, [fallback(build_plan(snapshot(), 0), 3)])
        model.future.set_result("[]")
        await asyncio.sleep(0)

    async def test_late_model_exception_is_consumed(self):
        model = FakeModel(pending=True)
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            await DecisionEngine(3, model=model, budget_seconds=0.02).decide({"teamId": 0, "gameState": snapshot()})
            model.future.set_exception(RuntimeError("late socket failure"))
            await asyncio.sleep(0.01)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_missing_authoritative_team_is_rejected(self):
        with self.assertLogs("total_football.runtime", level="ERROR"):
            result = await DecisionEngine(3, model=FakeModel("[]")).decide({"prompt": "{}"})
        self.assertEqual(result, [])

    async def test_empty_state_keeps_effective_id_in_last_resort(self):
        result = await DecisionEngine(3, model=FakeModel("[]")).decide({"teamId": 1, "myPlayers": [4]})
        self.assertEqual(result[0]["playerId"], 4)


class SingleFlightTests(unittest.TestCase):
    def test_stalled_call_does_not_spawn_more_workers(self):
        release = Event()
        worker = SingleFlightModel(3)
        worker._infer = lambda text: release.wait(1) or "[]"
        first = worker.start("one")
        try:
            with self.assertRaises(InferenceBusy):
                worker.start("two")
        finally:
            release.set()
            first.result(timeout=2)


class PackagingTests(unittest.TestCase):
    def test_five_runtimes_and_resolvable_bootstrap_paths(self):
        spec = json.loads((ROOT / "agentcore" / "agentcore.json").read_text(encoding="utf-8"))
        self.assertEqual(len(spec["runtimes"]), 5)
        self.assertEqual(spec["name"], "TeamStrandsTotalFootball")
        for runtime in spec["runtimes"]:
            entrypoint = ROOT / runtime["codeLocation"] / runtime["entrypoint"]
            self.assertTrue(entrypoint.is_file())
            self.assertEqual(entrypoint.parents[3] / "lib", ROOT.parent / "lib")

    def test_all_five_entrypoints_register_without_paid_inference(self):
        class App:
            logger = logging.getLogger("test-app")

            def entrypoint(self, fn):
                self.handler = fn
                return fn

        runtime = ModuleType("bedrock_agentcore.runtime")
        runtime.BedrockAgentCoreApp = App
        with patch.dict(sys.modules, {"bedrock_agentcore": ModuleType("bedrock_agentcore"),
                                      "bedrock_agentcore.runtime": runtime}):
            for pid, agent in enumerate(("ai-gk", "ai-def", "ai-mid", "ai-fwd1", "ai-fwd2")):
                loaded = runpy.run_path(str(ROOT / agent / "src" / "main.py"))
                self.assertEqual(loaded["engine"].pid, pid)
                self.assertTrue(callable(loaded["app"].handler))

    def test_packaging_contains_tactics_and_cleanup_preserves_source(self):
        spec = importlib.util.spec_from_file_location("team_deploy", ROOT / "deploy_all.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="football-package-") as temporary:
            directory = Path(temporary)
            team = directory / "team"
            for agent in module.AGENTS:
                (team / agent).mkdir(parents=True)
            (directory / "lib" / "total_football").mkdir(parents=True)
            source = directory / "lib" / "total_football" / "tactics.py"
            source.write_text("# packaging fixture\n", encoding="utf-8")
            with patch.object(module, "ROOT", team):
                created = module.prepare()
                self.assertEqual(len(created), 5)
                for path in created:
                    self.assertTrue((path / "total_football" / "tactics.py").is_file())
                with self.assertRaises(RuntimeError):
                    module.prepare()
                module.cleanup(created)
                self.assertTrue(source.is_file())
                self.assertTrue(all(not path.exists() for path in created))


if __name__ == "__main__":
    unittest.main()
