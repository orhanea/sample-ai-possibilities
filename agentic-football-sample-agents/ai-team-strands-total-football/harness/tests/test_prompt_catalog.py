"""Offline safeguards for refactoring and maintaining the five prompt strings."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from harness import prompt_catalog

HERE = Path(__file__).resolve().parents[1]


class PromptCompatibilityTests(unittest.TestCase):
    def test_refactor_preserves_all_five_model_facing_strings(self):
        expected = json.loads((HERE / "tests/fixtures/pre_refactor_hashes.json").read_text(encoding="utf-8"))
        actual = {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in prompt_catalog.render_prompts().items()
        }
        self.assertEqual(expected, actual)

    def test_checked_in_snapshots_match_their_source(self):
        prompt_catalog.require_current_snapshots()

    def test_command_examples_keep_the_right_player_and_numeric_targets(self):
        rendered = prompt_catalog.render_prompts()
        for player in prompt_catalog.load_players():
            examples = [line for line in rendered[player["name"]].splitlines() if line.startswith("[{")]
            self.assertTrue(examples, player["name"])
            for line in examples:
                with self.subTest(player=player["name"], example=line):
                    commands = json.loads(line)
                    self.assertEqual(len(commands), 1)
                    command = commands[0]
                    self.assertEqual(command["playerId"], player["player_id"])
                    self.assertIn(command["teamId"], (0, 1))
                    self.assertIs(type(command["duration"]), int)
                    if command["commandType"] in ("PASS", "GK_DISTRIBUTE"):
                        target = command["parameters"]["target_player_id"]
                        self.assertIs(type(target), int)
                        self.assertNotEqual(target, player["player_id"])
                    if command["commandType"] == "MOVE_TO":
                        self.assertIs(type(command["parameters"]["sprint"]), bool)

    def test_read_only_cli_works_outside_the_team_directory(self):
        result = subprocess.run(
            [sys.executable, str(HERE / "build_prompts.py"), "--check"],
            cwd=HERE / "tests", capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(": OK ("), 5)

    def test_stale_snapshot_blocks_the_active_publishing_preflight(self):
        with TemporaryDirectory(prefix="football-prompts-") as temporary:
            directory = Path(temporary)
            shutil.copytree(prompt_catalog.SOURCE, directory / "prompts")
            for name, text in prompt_catalog.render_prompts().items():
                (directory / f"{name}.txt").write_text(text, encoding="utf-8")
            prompt_catalog.require_current_snapshots(directory)
            (directory / "Johan_Cruyff.txt").write_text("outdated instructions", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Johan_Cruyff.txt"):
                prompt_catalog.require_current_snapshots(directory)

    def test_unknown_template_variable_fails_instead_of_reaching_a_player(self):
        with TemporaryDirectory(prefix="football-prompts-") as temporary:
            source = Path(temporary) / "prompts"
            shutil.copytree(prompt_catalog.SOURCE, source)
            path = source / "outfield.template.txt"
            path.write_text(path.read_text(encoding="utf-8") + "$unknown_rule", encoding="utf-8")
            with self.assertRaises(KeyError):
                prompt_catalog.render_prompts(source)


class PlayerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((prompt_catalog.SOURCE / "players.json").read_text(encoding="utf-8"))

    def assert_invalid(self, mutate):
        document = copy.deepcopy(self.document)
        mutate(document)
        with self.assertRaises(ValueError):
            prompt_catalog.validate_players(document)

    def test_rejects_missing_player(self):
        self.assert_invalid(lambda document: document["players"].pop())

    def test_rejects_duplicate_player_id(self):
        self.assert_invalid(lambda document: document["players"][2].update(player_id=1))

    def test_rejects_duplicate_resource_name(self):
        self.assert_invalid(lambda document: document["players"][2].update(name="Franz_Beckenbauer"))

    def test_rejects_boolean_instead_of_integer_id(self):
        self.assert_invalid(lambda document: document["players"][1].update(player_id=True))

    def test_rejects_file_path_in_player_name_or_role(self):
        for field in ("name", "role"):
            with self.subTest(field=field):
                self.assert_invalid(lambda document: document["players"][1].update({field: "../other"}))

    def test_goalkeeper_cannot_be_reassigned_to_an_outfielder(self):
        self.assert_invalid(lambda document: document["players"][1].update(role="goalkeeper"))
        self.assert_invalid(lambda document: document["players"][0].update(role="finisher"))

    def test_rejects_self_pass_string_and_boolean_targets(self):
        for target in (1, "agentId_2", True):
            with self.subTest(target=target):
                self.assert_invalid(lambda document: document["players"][1].update(own_end_example_target=target))

    def test_rejects_unknown_fields_and_schema_version(self):
        self.assert_invalid(lambda document: document["players"][1].update(player_index=1))
        self.assert_invalid(lambda document: document.update(version=2))


if __name__ == "__main__":
    unittest.main()
