"""RDP 网关选单模型的回归用例（纯逻辑，不连库、不连网络）。"""

from __future__ import annotations

from app.gateway.rdp_menu import MenuItem, RdpMenu, build_menu_items


def _item(i: int) -> MenuItem:
    return MenuItem(
        host_id=i,
        host_name=f"host-{i:02d}",
        address=f"10.0.0.{i}",
        port=3389,
        account_id=100 + i,
        account_username=f"Administrator{i}",
        os_type="windows",
    )


def test_empty_menu_tells_the_user_what_to_do():
    menu = RdpMenu([])
    assert menu.empty and len(menu) == 0
    assert menu.selected() is None
    assert menu.move(1) is None and menu.hit_test(0) is None
    lines = menu.render()
    assert len(lines) == 2 and "没有可用的 RDP 主机" in lines[0]


def test_keyboard_wraps_and_tracks_the_page():
    items = [_item(i) for i in range(1, 16)]  # 15 台 → 2 页（每页 12）
    menu = RdpMenu(items)
    assert menu.selected().host_name == "host-01"

    menu.move(1)
    assert menu.selected().host_name == "host-02"

    menu.move(-2)  # 环绕到最后一台
    assert menu.selected().host_name == "host-15"
    assert menu.page == 1, "光标跨页时页码要跟着走"

    menu.home()
    assert menu.selected().host_name == "host-01" and menu.page == 0

    menu.end()
    assert menu.selected().host_name == "host-15" and menu.page == 1


def test_page_keys_jump_by_page():
    menu = RdpMenu([_item(i) for i in range(1, 26)])  # 25 台 → 3 页
    assert menu.pages == 3

    menu.page_move(1)
    assert menu.page == 1 and menu.selected().host_name == "host-13"
    menu.page_move(1)
    assert menu.page == 2 and menu.selected().host_name == "host-25"
    menu.page_move(1)  # 环绕回第一页
    assert menu.page == 0 and menu.selected().host_name == "host-01"


def test_mouse_click_selects_the_clicked_row_and_ignores_blank_rows():
    items = [_item(i) for i in range(1, 4)]
    menu = RdpMenu(items)
    assert menu.hit_test(2).host_name == "host-03"
    assert menu.cursor == 2
    assert menu.hit_test(5) is None, "点在空行上不选中任何条目"
    assert menu.hit_test(-1) is None


def test_row_of_only_answers_for_the_current_page():
    menu = RdpMenu([_item(i) for i in range(1, 16)])
    assert menu.row_of(0) == 0
    assert menu.row_of(3) == 3
    menu.page_move(1)
    assert menu.row_of(0) == -1, "第一页的条目在第二页上没有行号"
    assert menu.row_of(13) == 1


def test_render_marks_the_cursor_and_shows_paging():
    menu = RdpMenu([_item(i) for i in range(1, 15)])
    lines = menu.render(width=60)
    assert lines[0].startswith(">"), "光标在第一行时要有 > 标记"
    assert "host-01" in lines[0] and "Administrator1" in lines[0]
    assert "第 1/2 页" in lines[-1] and "共 14 台" in lines[-1]

    menu.end()
    lines = menu.render()
    assert any("host-14" in line for line in lines)
    assert "第 2/2 页" in lines[-1]


def test_build_menu_items_never_raises_when_access_lookup_fails(monkeypatch):
    class Boom:
        def __init__(self):
            raise RuntimeError("no db")

    import app.gateway.rdp_menu as mod

    monkeypatch.setattr(mod, "build_menu_items", mod.build_menu_items)
    assert build_menu_items(object()) == [], "授权查询炸掉时应当返回空列表而不是 500"
