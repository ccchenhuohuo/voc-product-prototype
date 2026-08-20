import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from app.routes import board, home


SYSTEM_DIR = Path(__file__).resolve().parents[1]


def test_root_is_owned_by_home_router_not_iteration_router():
    board_paths = {route.path for route in board.router.routes}
    home_paths = {route.path for route in home.router.routes}
    assert "/" not in board_paths
    assert "/" in home_paths


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


def test_local_htmx_honors_login_redirect_without_swapping_error_body():
    runtime = (SYSTEM_DIR / "app/static/htmx.min.js").read_text()
    assert "HX-Redirect" in runtime
    assert "window.location.assign(redirect)" in runtime


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


def test_user_menu_shows_authenticated_user_and_real_logout_link():
    template = (SYSTEM_DIR / "app/templates/base.html").read_text()
    assert "data-user-popover" in template
    assert "飞书已登录" in template
    assert re.search(r'<a class="logout-link" href="/auth/logout">', template)
    assert "退出登录" in template


def test_navigation_has_four_business_items_and_no_product_search():
    template = (SYSTEM_DIR / "app/templates/base.html").read_text()
    sidebar = template.split('<aside class="sb">', 1)[1].split(
        '<div class="sb-footer">', 1
    )[0]
    # 两个一级分区（首页、数据架构），首页下挂三个二级业务页。
    assert sidebar.count("nav-lv1") == 2
    assert sidebar.count("nav-lv2") == 3
    assert sidebar.count('class="nav') == 5
    assert "产品检索" not in sidebar
    # 二级业务页各只挂一个读数，口径统一。
    assert "{{ iter_count }}" in sidebar and "product_count" not in sidebar


def test_removed_search_template_and_channel_ui_do_not_return():
    templates = SYSTEM_DIR / "app/templates"
    assert not (templates / "product-search.html").exists()
    innovation = "\n".join(
        (templates / name).read_text()
        for name in ("inno-list.html", "innovation-card.html")
    )
    home = (templates / "home.html").read_text()
    for text in (innovation, home):
        assert "channel" not in text.lower()
        assert "竞品对标" not in text
        assert "产品体验" not in text


def test_spu_sort_validation_has_one_shared_source():
    routes = SYSTEM_DIR / "app/routes"
    sources = {
        path.name: path.read_text()
        for path in routes.glob("*.py")
    }
    assert sum(text.count("SORT_KEYS =") for text in sources.values()) == 1
    assert sum(text.count("def sort_state(") for text in sources.values()) == 1
    assert "from .spu_table import sort_state" in sources["board.py"]
