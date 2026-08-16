import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import board


SYSTEM_DIR = Path(__file__).resolve().parents[1]


def test_root_redirects_to_iteration_queue():
    app = FastAPI()
    app.include_router(board.router)
    response = TestClient(app).get("/", follow_redirects=False)
    assert response.status_code in (302, 307, 308)
    assert response.headers["location"] == "/iter"


def test_old_notion_tokens_and_card_css_are_removed():
    tokens = (SYSTEM_DIR / "app/static/_tokens.css").read_text()
    css = (SYSTEM_DIR / "app/static/app.css").read_text()
    assert "--field" in tokens
    assert "--alarm" in tokens
    for old in ("--paper", "--side", "--accent", "--g-bg", "--b-bg", "--p-bg"):
        assert old not in tokens
    assert ".card" not in css
    assert "box-shadow" not in css


def test_keyboard_navigation_contract_is_present():
    script = (SYSTEM_DIR / "app/static/app.js").read_text()
    for token in ("scrollIntoView", 'block: "nearest"', ".sel", "tabindex", "Escape", "Enter"):
        assert token in script
    assert "input, textarea" in script or "input,textarea" in script


def test_local_htmx_honors_full_refresh_for_live_counts():
    runtime = (SYSTEM_DIR / "app/static/htmx.min.js").read_text()
    assert "HX-Refresh" in runtime
    assert "window.location.reload()" in runtime


def test_synchronous_theme_bootstrap_outputs_all_three_modes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the inline theme bootstrap")
    template = (SYSTEM_DIR / "app/templates/base.html").read_text()
    match = re.search(r"<script>\s*(.*?)\s*</script>", template, re.DOTALL)
    assert match
    source = match.group(1)
    harness = f"""
const vm = require("node:vm");
const source = {json.dumps(source)};
const results = {{}};
for (const theme of ["light", "dark", "system"]) {{
  const attrs = {{}};
  global.localStorage = {{ getItem: () => theme }};
  global.document = {{ documentElement: {{
    setAttribute: (name, value) => {{ attrs[name] = value; }},
    removeAttribute: (name) => {{ delete attrs[name]; }}
  }} }};
  vm.runInThisContext(source);
  results[theme] = attrs["data-theme"] ?? null;
}}
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "-e", harness], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == {
        "light": "light", "dark": "dark", "system": None,
    }


def test_theme_tokens_support_explicit_and_system_modes_without_new_colors():
    tokens = (SYSTEM_DIR / "app/static/_tokens.css").read_text()
    assert ':root:not([data-theme="light"])' in tokens
    assert ':root[data-theme="dark"]' in tokens
    assert tokens.count("--field: #0f0f11") == 2


def test_user_menu_keeps_logout_as_disabled_placeholder():
    template = (SYSTEM_DIR / "app/templates/base.html").read_text()
    assert "data-user-popover" in template
    assert "角色 / 邮箱待接入" in template
    assert re.search(r'<button class="logout-placeholder"[^>]* disabled>', template)
    assert "退出登录 <span>待接入</span>" in template
