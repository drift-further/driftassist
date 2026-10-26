"""Script Content-Security-Policy (effort 621, Phase 2).

Escaping is the only thing between a string that reaches innerHTML and a host
shell plus the plaintext vault. `script-src 'self'` is the second layer: an
injected `<img onerror=…>` or `<script>` does not run. It only holds while the
app itself has no inline script — every `on*=` attribute became a
`data-action` resolved through js/actions-registry.js, the two inline scripts
moved to files, and the login page's approval script is served at /login.js
(exempt from auth, because a signed-out phone is exactly who needs it).
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

import shared.auth as auth
from shared import auth_gate, security

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
REGISTRY = ROOT / "js" / "actions-registry.js"
TOKEN = "test-token-not-the-real-one"
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)


def _login_markup():
    from routes import static

    return static._LOGIN_PAGE + static._REQUEST_UI


class NoInlineCodeTests(unittest.TestCase):
    def test_no_inline_handlers_remain(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertEqual(_INLINE_HANDLER.findall(html), [])
        self.assertEqual(_INLINE_HANDLER.findall(_login_markup()), [])
        # Must-fire: the pattern does catch one.
        self.assertTrue(_INLINE_HANDLER.search('<button onclick="x()">'))

    def test_no_inline_scripts_remain(self):
        self.assertEqual(_INLINE_SCRIPT.findall(INDEX.read_text(encoding="utf-8")), [])
        self.assertEqual(_INLINE_SCRIPT.findall(_login_markup()), [])
        self.assertTrue(_INLINE_SCRIPT.search("<script>alert(1)</script>"))
        self.assertFalse(_INLINE_SCRIPT.search('<script src="/js/a.js"></script>'))

    def test_moved_scripts_are_loaded(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('<script src="/js/prepaint.js', html)
        self.assertLess(html.index("/js/prepaint.js"), html.index('class="chrome-bar') if 'class="chrome-bar' in html else len(html))
        self.assertIn("serviceWorker", (ROOT / "js" / "app.js").read_text(encoding="utf-8"))
        self.assertIn('<script src="/login.js', _login_markup())
        # The registry loads before any module whose functions it names.
        scripts = re.findall(r'<script src="/js/([a-z0-9-]+)\.js', html)
        self.assertEqual(scripts[0:2], ["prepaint", "actions-registry"])

    def test_no_js_module_builds_inline_handlers(self):
        for path in sorted((ROOT / "js").glob("*.js")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"""['"\s]on(click|change|input|keydown|submit|error|load)=""", text),
                path.name,
            )
            self.assertIsNone(re.search(r"\beval\(|new Function\(", text), path.name)


# ---------------------------------------------------------------------------
# The registry, under Node
# ---------------------------------------------------------------------------

_HARNESS = r"""
const listeners = {};
global.document = {
  addEventListener(type, fn, capture) { (listeners[type] = listeners[type] || []).push(fn); },
};
global.window = global;
const calls = [];
__STUBS__
__REGISTRY__
function el(data, parent) {
  return { dataset: data, parentElement: parent || null, tagName: 'BUTTON' };
}
function fire(type, target, extra) {
  const ev = Object.assign({ type, target, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } }, extra || {});
  for (const fn of (listeners[type] || [])) fn(ev);
  return ev;
}
const scenario = process.argv[2];
if (scenario === 'keys') {
  console.log(JSON.stringify(Object.keys(ACTIONS)));
} else if (scenario === 'element') {
  const input = el({ change: 'onFileSelected' });
  fire('change', input);
  const search = el({ input: 'onTermSearch' });
  fire('input', search);
  console.log(JSON.stringify({ gotElement: calls[0] && calls[0][1] === input, calls: calls.map(c => c[0]) }));
} else if (scenario === 'click') {
  const outer = el({ action: 'toggleActionsDeck' });
  const inner = el({ action: 'fitMenuPick', arg: '58', then: 'closeActionsDeck' }, outer);
  const child = { dataset: {}, parentElement: inner };
  fire('click', child);
  fire('click', el({ action: 'typePrefix', arg: '"@"' }));
  fire('click', el({ action: 'noSuchAction' }));
  console.log(JSON.stringify(calls.map(c => [c[0], c[2]])));
}
"""


def _run_node(scenario, stub_names):
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    stubs = "\n".join(
        f"global.{name} = function () {{ calls.push(['{name}', arguments[0], arguments[0]]); }};"
        for name in stub_names
    )
    # SplitView.openPicker is a method, not a global function.
    stubs += "\nglobal.SplitView = { openPicker() { calls.push(['SplitView.openPicker']); } };"
    source = _HARNESS.replace("__STUBS__", stubs).replace(
        "__REGISTRY__", REGISTRY.read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory() as raw:
        script = Path(raw) / "harness.js"
        script.write_text(source)
        out = subprocess.run(
            ["node", str(script), scenario], capture_output=True, text=True, timeout=20
        )
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def _declared(html):
    names = []
    for attr in ("action", "then", "input", "change", "keydown", "submit"):
        names += re.findall(rf'\sdata-{attr}="([^"]+)"', html)
    return names


class RegistryTests(unittest.TestCase):
    def test_every_data_action_is_registered(self):
        html = INDEX.read_text(encoding="utf-8")
        declared = set(_declared(html))
        self.assertGreater(len(declared), 80)
        keys = set(_run_node("keys", []))
        self.assertEqual(declared - keys, set(), "data-action with no registry entry")
        # Every data-arg is a JSON literal the registry can parse.
        for raw in re.findall(r"\sdata-arg='([^']*)'", html) + re.findall(r'\sdata-arg="([^"]*)"', html):
            json.loads(raw.replace("&quot;", '"'))

    def test_input_and_change_actions_receive_element(self):
        result = _run_node("element", ["onFileSelected", "onTermSearch"])
        self.assertTrue(result["gotElement"])
        self.assertEqual(result["calls"], ["onFileSelected", "onTermSearch"])

    def test_click_bubbles_through_nested_actions_with_args_and_then(self):
        result = _run_node(
            "click", ["fitMenuPick", "closeActionsDeck", "toggleActionsDeck", "typePrefix"]
        )
        self.assertEqual(
            result,
            [["fitMenuPick", 58], ["closeActionsDeck", None], ["toggleActionsDeck", None],
             ["typePrefix", "@"]],
        )


# ---------------------------------------------------------------------------
# The header, through the real gate and static routes
# ---------------------------------------------------------------------------


class HeaderTests(unittest.TestCase):
    def setUp(self):
        saved = auth._token_cache
        auth._token_cache = TOKEN
        self.addCleanup(setattr, auth, "_token_cache", saved)
        from routes.access import access_bp
        from routes.static import static_bp

        app = Flask(__name__)
        auth_gate.register_auth_gate(app)
        security.register_csp(app)
        app.register_blueprint(static_bp)
        app.register_blueprint(access_bp)
        self.client = app.test_client()

    def _get(self, path, authed=True):
        headers = {auth.HEADER_NAME: TOKEN} if authed else {}
        return self.client.get(path, headers=headers)

    def test_csp_header_enforced_on_index_login_loginjs_and_static(self):
        for path in ("/", "/login", "/login.js", "/js/app.js", "/css/base.css",
                     "/js/actions-registry.js", "/js/prepaint.js", "/sw.js"):
            r = self._get(path, authed=path not in ("/login", "/login.js"))
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("Content-Security-Policy"), POLICY, path)
            self.assertNotIn("Content-Security-Policy-Report-Only", r.headers, path)
        # Error responses carry it too.
        r = self._get("/js/nope.js")
        self.assertEqual(r.headers.get("Content-Security-Policy"), POLICY)

    def test_report_only_when_setting_off(self):
        from shared import state

        settings = state.get_settings()
        settings["security"] = {"csp_enforce": False}
        with mock.patch.object(state, "get_settings", return_value=settings):
            r = self._get("/login", authed=False)
        self.assertEqual(r.headers.get("Content-Security-Policy-Report-Only"), POLICY)
        # frame-ancestors is not honoured in a report-only policy, so the
        # enforcing header keeps it alone.
        self.assertEqual(r.headers.get("Content-Security-Policy"), "frame-ancestors 'none'")
        self.assertNotIn("report-uri", POLICY)
        self.assertNotIn("report-to", POLICY)

    def test_default_setting_enforces(self):
        from shared import state

        self.assertIs(state.DEFAULT_SETTINGS["security"]["csp_enforce"], True)

    def test_login_js_exempt_and_js_route_still_guarded(self):
        self.assertEqual(self._get("/login", authed=False).status_code, 200)
        r = self._get("/login.js", authed=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["Content-Type"])
        self.assertIn("/access/request/status", r.get_data(as_text=True))
        for path in ("/js/app.js", "/js/actions-registry.js", "/"):
            self.assertIn(self._get(path, authed=False).status_code, (302, 401), path)
        self.assertIn("static_bp.login_script", auth_gate.AUTH_EXEMPT)
        self.assertEqual(
            set(auth_gate.AUTH_EXEMPT),
            {"static_bp.login", "static_bp.login_script", "poll_bp.cli_proxy",
             "poll_bp.health", "access_bp.access_request", "access_bp.access_request_status"},
        )

    def test_serve_registers_csp(self):
        source = (ROOT / "serve.py").read_text(encoding="utf-8")
        self.assertIn("security.register_csp(app)", source)
        self.assertNotIn("Content-Security-Policy\"] = \"frame-ancestors", source)


if __name__ == "__main__":
    unittest.main()
