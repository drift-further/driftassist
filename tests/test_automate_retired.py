"""Automate and Container are retired (effort 621 phase 6, question 4867: drop).

They were parked behind the execution park for months; the owner chose to drop
them rather than un-park. What must hold afterwards, in both directions:

- no `/api/automate/*` or `/api/container/*` route exists in the real app
  factory, while the routes that stayed (the parked `/api/cli-proxy`, settings,
  project settings) still answer;
- the execution park lost only the six Automate intents: the two configured
  intents stay denied and the allowed set is exactly what it was;
- `/poll` carries no automate block and startup runs no Automate recovery;
- a project-settings PATCH carrying retired keys is refused, while an old
  install's files (`container_config.json`, `automate_state.json`, Automate keys
  in `project_settings.json`) are neither read nor deleted.
"""

import ast
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

# Scratch roots before anything imports shared.state, so the module also runs
# on its own (`python -m unittest tests.test_automate_retired`): its PATCH tests
# write project_settings.json, which must never be a real install's.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()

from flask import Flask  # noqa: E402

import shared.auth as auth  # noqa: E402
from shared import execution_park as park  # noqa: E402
from shared import paths  # noqa: E402
from shared import state  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-not-the-real-one"


def _serve_namespace():
    """serve.py's imports and functions, without its module-level start-up.

    Importing serve.py as a module runs `create_app()` AND
    `start_application_backgrounds()` (its `__name__ != "__main__"` block), which
    would start the scanner threads inside the test process.
    """
    tree = ast.parse((ROOT / "serve.py").read_text(encoding="utf-8"))
    tree.body = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
    ]
    namespace = {"__name__": "serve_under_test", "__file__": str(ROOT / "serve.py")}
    exec(compile(tree, str(ROOT / "serve.py"), "exec"), namespace)
    return namespace


def _load_cli():
    loader = SourceFileLoader("assist_cli_retired", str(ROOT / "bin" / "assist"))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
        loader.exec_module(module)
    return module


class _RealAppMixin:
    def setUp(self):
        saved = auth._token_cache
        auth._token_cache = TOKEN
        self.addCleanup(setattr, auth, "_token_cache", saved)
        self.app = _serve_namespace()["create_app"]()
        self.app.logger.disabled = True
        self.client = self.app.test_client()

    def _headers(self):
        return {auth.HEADER_NAME: TOKEN, "Host": "127.0.0.1"}


class RoutesRetiredTests(_RealAppMixin, unittest.TestCase):
    def test_no_automate_or_container_routes_registered(self):
        rules = sorted(rule.rule for rule in self.app.url_map.iter_rules())
        self.assertEqual(
            [r for r in rules if r.startswith(("/api/automate", "/api/container"))], []
        )
        for method, path in (
            ("post", "/api/automate/start"),
            ("post", "/api/automate/stop"),
            ("post", "/api/automate/patch"),
            ("get", "/api/automate/status"),
            ("get", "/api/container/status"),
            ("post", "/api/container/build"),
            ("get", "/api/container/config"),
            ("get", "/api/container/extensions"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, headers=self._headers())
                self.assertEqual(response.status_code, 404)
        # Must-not-fire: the gate let the request through (a 401 would also be
        # "not a 200"), and neighbouring routes in the same app still answer.
        self.assertEqual(
            self.client.get("/api/settings", headers=self._headers()).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/project-settings/demo", headers=self._headers()).status_code,
            200,
        )
        for module in ("routes/automate.py", "routes/container.py",
                       "cli/container.py", "js/container.js", "css/container.css"):
            with self.subTest(module=module):
                self.assertFalse((ROOT / module).exists())

    RETIRED_PATHS = (
        ("post", "/api/automate/start"),
        ("get", "/api/automate/status"),
        ("post", "/api/automate/stop"),
        ("get", "/api/container/status"),
        ("post", "/api/container/build"),
        ("patch", "/api/container/config"),
        ("get", "/api/container/extensions"),
        ("post", "/api/container/kill/x"),
    )

    def test_retired_paths_still_behind_the_auth_gate(self):
        # No credential: the gate answers before routing could say 404, so an
        # unrouted path is no unauthenticated probe surface.
        for method, path in self.RETIRED_PATHS:
            with self.subTest(path=path, accept="json"):
                response = getattr(self.client, method)(path, headers={"Host": "127.0.0.1"})
                self.assertEqual(response.status_code, 401)
            with self.subTest(path=path, accept="html"):
                response = getattr(self.client, method)(
                    path, headers={"Host": "127.0.0.1", "Accept": "text/html"}
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login")
        # Must-not-fire: with the token the same paths reach routing (404).
        for method, path in self.RETIRED_PATHS:
            with self.subTest(path=path, accept="token"):
                response = getattr(self.client, method)(path, headers=self._headers())
                self.assertEqual(response.status_code, 404)

    def test_cli_proxy_still_parked_409(self):
        with mock.patch("routes.poll.subprocess.run") as run:
            response = self.client.post(
                "/api/cli-proxy", json={"args": ["anything"]}, headers=self._headers()
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["intent"], "configured_cli_proxy")
        self.assertEqual(response.get_json()["error"], "container_launch_parked")
        run.assert_not_called()


class ParkIntentTests(unittest.TestCase):
    ALLOWED_BEFORE = {
        "terminal_init_launch", "terminal_init_duplicate", "terminal_run_init",
        "configured_restart", "saved_command", "project_venv", "fixed_git",
        "native_folder_picker", "autoyes_answer", "operator_interactive",
        "client_session_resume", "client_session_restart", "bare_terminal",
        "observe", "stop", "image_config",
    }

    def test_allowed_intent_set_unchanged_after_removal(self):
        self.assertEqual({i.value for i in park.ALLOWED_INTENTS}, self.ALLOWED_BEFORE)
        self.assertEqual(
            {i.value for i in park.DENIED_INTENTS},
            {"configured_cli_proxy", "configured_image_build"},
        )
        self.assertEqual(
            [i.name for i in park.Intent if i.name.startswith("AUTOMATE_")], []
        )
        # The two that stay denied still refuse before their effect runs ...
        ran = []
        for intent in park.DENIED_INTENTS:
            with self.subTest(intent=intent.value):
                result = park.ExecutionPark(park.Phase.PARKED).perform(
                    intent, lambda: ran.append(intent)
                )
                self.assertTrue(park.is_refusal(result))
        self.assertEqual(ran, [])
        # ... and an allowed one still runs it.
        self.assertEqual(
            park.ExecutionPark(park.Phase.PARKED).perform(park.Intent.OBSERVE, lambda: "ran"),
            "ran",
        )

    def test_no_parked_flag_left_for_nothing_to_read(self):
        self.assertFalse(hasattr(park, "FEATURES_PARKED"))
        for path in ("routes/settings.py", "cli/proc.py", "bin/assist", "install.sh",
                     "js/state.js", "index.html"):
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertNotIn("FEATURES_PARKED", text)
                self.assertNotIn("data-parked", text)


class HelpTests(unittest.TestCase):
    def test_help_lists_no_container_verbs(self):
        cli = _load_cli()
        text = cli.help_text()
        self.assertNotIn("container", text.lower())
        self.assertNotIn("container", cli.COMMANDS)
        # Must-not-fire: the help still lists the verbs that stayed.
        for verb in ("ls [", "send SESSION", "send-file", "doctor", "devices"):
            self.assertIn(verb, text)

    def test_container_verb_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            roots = {}
            for key, name in (("ASSIST_CONFIG_DIR", "config"), ("ASSIST_DATA_DIR", "data"),
                              ("ASSIST_STATE_DIR", "state")):
                roots[key] = Path(root) / name
                roots[key].mkdir()
            env = _paths.scratch_env(root, **roots)
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "assist"), "container", "status"],
                capture_output=True, text=True, timeout=30, env=env,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown command: container", result.stderr)


class PollTests(unittest.TestCase):
    def test_poll_has_no_automate_block(self):
        from routes import poll

        self.assertFalse(hasattr(state, "automate"))
        self.assertFalse(hasattr(state, "automate_lock"))
        patches = [
            mock.patch.object(state, "tmux_target", None),
            mock.patch("routes.poll.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 1, "", "")),
            mock.patch("routes.poll.tab_state.sweep_agent_declarations"),
            mock.patch("routes.poll.tab_state.sweep_wakes"),
            mock.patch("routes.poll.tab_state.apply_order", side_effect=lambda panes: panes),
            mock.patch("routes.poll.tab_state.get_tab_state", return_value={}),
            mock.patch("routes.poll.drafts.poll_block", return_value=None),
            mock.patch("routes.studio.studio_poll_block", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        body = app.test_client().get("/poll").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("automate", body)


class StartupTests(unittest.TestCase):
    def test_startup_has_no_automate_recovery(self):
        seeded = state.DATA_DIR / "automate_state.json"
        payload = json.dumps({"active": True, "session": "auto-x", "status": "running"})
        seeded.write_text(payload)
        self.addCleanup(seeded.unlink, missing_ok=True)
        sys.modules.pop("routes.automate", None)

        namespace = _serve_namespace()
        with mock.patch.dict(namespace, {"threading": mock.MagicMock()}), \
                mock.patch("routes.drafts.start_sweeper"), \
                mock.patch("routes.outbox.start_sweeper"), \
                mock.patch("routes.autoyes.restore_autoyes_from_settings") as restore, \
                mock.patch("shared.updates.manager"):
            namespace["start_application_backgrounds"]()
        # Must-fire: the start-up ran (the AutoYes restore is still in it) ...
        restore.assert_called_once()
        # ... and touched nothing of Automate's.
        self.assertNotIn("routes.automate", sys.modules)
        self.assertEqual(seeded.read_text(), payload)
        self.assertNotIn("automate", (ROOT / "serve.py").read_text().lower())


class ProjectSettingsTests(unittest.TestCase):
    def setUp(self):
        from routes import settings

        self._saved = copy.deepcopy(state._project_settings)
        self.addCleanup(self._restore)
        app = Flask(__name__)
        app.register_blueprint(settings.settings_bp)
        self.client = app.test_client()

    def _restore(self):
        with state._project_settings_lock:
            state._project_settings.clear()
            state._project_settings.update(self._saved)

    def test_project_settings_patch_rejects_automate_keys(self):
        for retired in ("automate", "triggers", "packages"):
            with self.subTest(key=retired):
                self.assertNotIn(retired, state.DEFAULT_PROJECT_SETTINGS)
                response = self.client.patch(
                    "/api/project-settings/demo", json={retired: {"timeout": 5}}
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["rejected"], [retired])
        # Mixed with a live key: the live key lands, the retired one is named.
        response = self.client.patch(
            "/api/project-settings/demo",
            json={"automate": {"timeout": 5}, "autoyes": {"delay": 3}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["rejected"], ["automate"])
        self.assertEqual(body["settings"]["autoyes"]["delay"], 3)
        saved = json.loads(state.PROJECT_SETTINGS_FILE.read_text())
        self.assertNotIn("automate", saved["demo"])

    def test_old_install_keys_survive_the_real_loader_and_a_patch(self):
        saved_file = (state.PROJECT_SETTINGS_FILE.read_bytes()
                      if state.PROJECT_SETTINGS_FILE.exists() else None)

        def restore_file():
            if saved_file is None:
                state.PROJECT_SETTINGS_FILE.unlink(missing_ok=True)
            else:
                state.PROJECT_SETTINGS_FILE.write_bytes(saved_file)
            state.load_project_settings()

        self.addCleanup(restore_file)
        retired = {
            "automate": {"timeout": 7, "default_prompt": "go"},
            "triggers": {"done_signals": ["DONE"], "done_idle_sec": 30},
            "packages": {"pip": ["requests"]},
        }
        state.PROJECT_SETTINGS_FILE.write_text(
            json.dumps({"legacy": {**retired, "autoyes": {"delay": 4}}})
        )
        state.load_project_settings()          # the path an old install's file takes
        response = self.client.patch(
            "/api/project-settings/legacy", json={"autoyes": {"delay": 9}}
        )
        self.assertEqual(response.status_code, 200)
        on_disk = json.loads(state.PROJECT_SETTINGS_FILE.read_text())["legacy"]
        self.assertEqual(on_disk["autoyes"]["delay"], 9)   # the PATCH did write
        for key, value in retired.items():
            with self.subTest(key=key):
                self.assertEqual(on_disk[key], value)

    def test_old_install_files_are_kept_and_not_read(self):
        # Automate keys an old install saved survive a later PATCH (the server
        # never deletes user data), and nothing loads container_config.json.
        with state._project_settings_lock:
            state._project_settings["old"] = {"automate": {"timeout": 7}}
        response = self.client.patch("/api/project-settings/old", json={"autoyes": {"delay": 4}})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(state.PROJECT_SETTINGS_FILE.read_text())
        self.assertEqual(saved["old"]["automate"], {"timeout": 7})
        for name in ("DEFAULT_CONTAINER_CONFIG", "CONTAINER_CONFIG_FILE",
                     "load_container_config", "AUTOMATE_STATE_FILE", "load_extensions",
                     "CLAUDE_MOUNT_SCRIPT"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(state, name))
        source = (ROOT / "shared" / "state.py").read_text()
        self.assertNotIn("container_config.json", source)
        self.assertNotIn("automate_state.json", source)


class RetiredStateItemsTests(unittest.TestCase):
    RETIRED = ("container_config.json", "extensions.json", "automate_state.json")

    def test_retired_files_stay_in_the_migration_inventory(self):
        items = {legacy: (root, name) for legacy, root, name in paths.STATE_ITEMS}
        for name in self.RETIRED:
            with self.subTest(name=name):
                self.assertEqual(items.get(name), ("data", name))

    def test_journal_naming_a_retired_file_still_validates(self):
        from cli import migrate_state

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            checkout, config, data, state_root = (base / n for n in ("co", "cfg", "data", "st"))
            marker = {
                "schema": paths.MARKER_SCHEMA, "direction": "forward", "phase": "copying",
                "checkout": str(checkout), "config_dir": str(config), "data_dir": str(data),
                "state_dir": str(state_root), "install_json": str(config / "install.json"),
                "token_path": None,
                "items": [{
                    "label": "automate_state.json",
                    "src": str(checkout / "automate_state.json"),
                    "dst": str(data / "automate_state.json"),
                    "kind": "file", "sha256": "a" * 64, "src_sha256": "a" * 64,
                }],
            }
            migrate_state._validate_journal(marker)   # an older build's journal resumes
            # Must-fire: a label outside the inventory is still refused.
            marker["items"][0]["label"] = "not_a_state_item.json"
            with self.assertRaises(migrate_state.MigrationError):
                migrate_state._validate_journal(marker)


if __name__ == "__main__":
    unittest.main()
