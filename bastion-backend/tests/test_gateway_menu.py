"""网关菜单排版守卫（真实缺陷回归）。

三个真实缺陷，用户侧分别是「提示文字错位」「列被中文顶歪」「= 号横贯整屏且全白」：

1. **裸 LF**：帮助块曾是无换行归一化的字符串（内部为 ``"\\n"``），而菜单其余部分是
   CRLF。PTY 里裸 LF 只把光标下移一行、**不回列**，于是每一行都从上一行结束的那一列
   开始打印，整块被排成阶梯状。任何写进通道的多行文本都必须走 ``to_crlf()``。
2. **按字符个数补齐**：``f"{hostName:<18}"`` 用的是 Python 的 ``ljust``（按字符数），
   而 CJK 在终端占 **2 列**，中文主机名/账号名会把后面的「策略」「账号」列顶歪。
   必须用 ``pad_display()`` 按显示列宽补齐。
3. **分隔线写死 78 列 + 全白无重点**：用户截图指出「这个 = 号要跟随文字长度变化，
   要不然不美观」「还有全是白色，太没有色彩了」。现在 ``=`` 的长度由紧邻内容块的
   显示宽度算出；颜色是 SGR 零宽序列，``display_width()`` 必须先剥掉 ``\\x1b[..m``，
   否则中英混排的列会立刻歪掉 —— 这条是加色之后最容易踩的坑。
"""

from __future__ import annotations

import re

from app.gateway.server import (
    BANNER_GAP,
    BANNER_SHIELD,
    BANNER_WORDMARK,
    BANNER_WORDMARK_OFFSET,
    CLR_BOLD,
    CLR_CYAN,
    DEFAULT_ROOM_WIDTH,
    HELP_ITEMS,
    _banner_rows,
    _help_text,
    _render_banner,
    _render_menu,
    _render_target_lines,
    _render_targets,
    color,
    display_width,
    pad_display,
    strip_ansi,
    to_crlf,
)

BARE_LF = re.compile(r"(?<!\r)\n")
BARE_CR = re.compile(r"\r(?!\n)")
DESCRIPTION_COLUMN = 16  # 4 空格缩进 + 12 列按键列


def _entry(**kwargs) -> dict:
    entry = {
        "hostId": 1,
        "hostName": "db-01",
        "address": "10.0.0.12",
        "port": 22,
        "groupName": "-",
        "policyName": "默认策略·高危命令拦截",
        "accounts": [],
        "canWebterm": True,
    }
    entry.update(kwargs)
    return entry


def _menu_segments(menu: str) -> tuple[list[list[str]], list[int]]:
    """把菜单按分隔线切成内容块，返回 (内容块, 分隔线长度列表)。"""
    segments: list[list[str]] = [[]]
    rules: list[int] = []
    for line in menu.split("\r\n"):
        plain = strip_ansi(line)
        if plain and set(plain) == {"="}:
            rules.append(len(plain))
            segments.append([])
        elif plain:
            segments[-1].append(plain)
    return [segment for segment in segments if segment], rules


def _block_width(block: list[str]) -> int:
    return max(display_width(line) for line in block)


# ---------------------------------------------------------------- 换行归一化 ---
def test_to_crlf_normalizes_every_line_ending():
    assert to_crlf("a\nb\r\nc\rd") == "a\r\nb\r\nc\r\nd"
    assert BARE_LF.search(to_crlf("a\nb")) is None
    assert BARE_CR.search(to_crlf("a\nb")) is None


def test_help_text_uses_crlf_only():
    text = _help_text()
    assert BARE_LF.search(text) is None, "帮助块出现裸 LF：PTY 下会把排版排成阶梯"
    assert BARE_CR.search(text) is None
    assert len(text.split("\r\n")) == len(HELP_ITEMS) + 1  # 标题 + 每个按键一行


def test_menu_has_no_bare_line_ending_anywhere(app):
    with app.app_context():
        menu = _render_menu(
            [_entry(), _entry(hostId=2, hostName="中文主机甲", accounts=[{"id": 1, "name": "中文账号"}])],
            "ops",
            "运维甲",
            "运维",
            "127.0.0.1",
            2,
        )
    assert BARE_LF.search(menu) is None, "菜单里出现裸 LF（帮助块/主机列表都必须走 CRLF）"
    assert BARE_CR.search(menu) is None
    assert menu.endswith("\r\n")


def test_banner_uses_crlf_only_on_every_terminal_width():
    for width in (DEFAULT_ROOM_WIDTH, 100, 80):
        banner = _render_banner(width)
        assert banner, f"{width} 列终端应该能看到字符画"
        assert BARE_LF.search(banner) is None
        assert BARE_CR.search(banner) is None


# ------------------------------------------------------------------ 列对齐 ---
def test_display_width_counts_cjk_as_two_columns():
    assert display_width("abc") == 3
    assert display_width("中文") == 4
    assert display_width("策略") == 4
    assert display_width("策略: ab") == 4 + 4  # 2 个宽字符 4 列 + ": ab" 4 列


def test_display_width_ignores_ansi_color_sequences():
    """颜色序列是零宽的：不剥掉就会把每个取值的列宽算大 5~10 列，中英混排立刻歪。"""
    assert display_width(color("中文abc", CLR_CYAN)) == 4 + 3
    assert display_width(color("中文", CLR_CYAN, CLR_BOLD)) == 4
    assert strip_ansi(color("中文abc", CLR_CYAN)) == "中文abc"


def test_pad_display_pads_by_columns_not_characters():
    assert pad_display("中文", 8) == "中文    "  # 4 列 + 4 空格 = 8 列
    assert display_width(pad_display("中文", 8)) == 8
    assert pad_display("ab", 8, "right") == "      ab"
    assert pad_display("超长内容超长内容", 4) == "超长内容超长内容"  # 超宽不截断


def test_pad_display_with_color_keeps_columns():
    """带色文本补齐后，可见列宽必须正好是目标宽度（颜色不占列）。"""
    padded = pad_display(color("中文ab", CLR_CYAN), 20)
    assert display_width(padded) == 20
    assert strip_ansi(padded) == "中文ab" + " " * 14  # 6 显示列 + 14 空格 = 20


def test_help_descriptions_start_at_the_same_column():
    lines = _help_text().split("\r\n")
    assert strip_ansi(lines[0]) == "  可用命令："
    assert "\x1b[" in lines[0], "帮助块标题应当上色（用户要求突出重点信息）"
    for (keys, description), line in zip(HELP_ITEMS, lines[1:], strict=True):
        plain = strip_ansi(line)
        assert plain.endswith(description)
        prefix = plain[: len(plain) - len(description)]
        assert prefix == "    " + pad_display(keys, 12)
        assert display_width(prefix) == DESCRIPTION_COLUMN, f"「{keys}」的说明没有落在第 {DESCRIPTION_COLUMN} 列"


def test_target_rows_keep_columns_aligned_with_cjk_content():
    entries = [
        _entry(hostName="中文主机甲", groupName="生产环境", policyName="只读审计策略（中文）"),
        _entry(hostId=2, hostName="db-01", groupName="-", policyName="默认策略·高危命令拦截"),
        _entry(hostId=3, hostName="web", groupName="测试", policyName="短"),
    ]
    rows = [strip_ansi(row) for row in _render_target_lines(entries)]
    policy_columns = {display_width(row[: row.index(" 策略: ")]) for row in rows}
    account_columns = {display_width(row[: row.index(" 账号: ")]) for row in rows}
    assert len(policy_columns) == 1, f"「策略」列不齐：{sorted(policy_columns)}"
    assert len(account_columns) == 1, f"「账号」列不齐：{sorted(account_columns)}"


def test_target_rows_are_colored_but_labels_stay_plain():
    """配色只加在取值上：标签保持纯文本，列对齐断言才能直接对可见文本做判断。"""
    row = _render_targets([_entry(accounts=[{"id": 1, "name": "root"}])])
    assert "\x1b[" in row, "主机行必须上色"
    assert " 策略: " in row and " 账号: " in row, "标签不该被颜色序列打断"
    assert strip_ansi(row).count("\r\n") == 1


def test_empty_target_list_is_a_single_crlf_line():
    text = _render_targets([])
    assert text.endswith("\r\n")
    assert BARE_LF.search(text) is None
    assert "没有你可访问的主机" in strip_ansi(text)


# -------------------------------------------------------------- 窄终端适配 ---
def test_wide_terminal_keeps_single_line_per_host():
    entries = [_entry(accounts=[{"id": 1, "name": "root"}, {"id": 2, "name": "deploy"}])]
    rows = _render_target_lines(entries, width=120)
    assert len(rows) == 1
    plain = strip_ansi(rows[0])
    assert " 策略: " in plain and " 账号: " in plain
    assert display_width(plain) <= 119


def test_narrow_terminal_uses_two_line_layout_fitting_the_width():
    """80 列是 PuTTY 默认宽度：完整单行约 95 列会被终端折行，必须换成两行式。"""
    entries = [
        _entry(hostName="中文主机甲", groupName="生产环境", policyName="默认策略·高危命令拦截",
               accounts=[{"id": 1, "name": "root"}, {"id": 2, "name": "deploy"}]),
        _entry(hostId=2, hostName="web-01", groupName="-", policyName="只读审计策略"),
    ]
    rows = _render_target_lines(entries, width=80)
    assert len(rows) == 4, "每台主机在窄终端下应占两行（主机行 + 明细行）"
    for row in rows:
        assert display_width(row) <= 80, f"窄终端下这行会折行：{row!r}（{display_width(row)} 列）"
    plain = [strip_ansi(row) for row in rows]
    assert "策略: 默认策略·高危命令拦截" in plain[1] and "账号: root、deploy" in plain[1]
    assert "分组: 生产环境" in plain[1]
    assert "-" not in plain[3], "分组为空时不应把占位符 '-' 打进明细行"


# ---------------------------------------------------------- 分隔线跟随内容 ---
def test_menu_separator_length_follows_the_content_width(app):
    """用户原话：「这个 = 号要跟随文字长度变化 要不然不美观」。"""
    with app.app_context():
        menu = _render_menu(
            [
                _entry(hostName="PVE_Linux", address="192.168.0.111", groupName="默认分组",
                       accounts=[{"id": 1, "name": "ubuntuserver"}]),
                _entry(hostId=2, hostName="demo-db-01", groupName="-"),
            ],
            "ops",
            "超级管理员",
            "系统管理员",
            "127.0.0.1",
            0,
            width=120,
        )
    blocks, rules = _menu_segments(menu)
    assert len(blocks) == 3, "菜单应分成「身份信息 / 可访问主机 / 可用命令」三段"
    assert len(rules) == len(blocks) + 1, "每段内容前后各一条分隔线"
    widths = [_block_width(block) for block in blocks]
    assert rules[0] == widths[0], "首条分隔线必须等于身份信息块的内容宽度"
    assert rules[1] == max(widths[0], widths[1])
    assert rules[2] == max(widths[1], widths[2])
    assert rules[3] == widths[2], "末条分隔线必须等于帮助块的内容宽度"
    assert rules[0] != rules[3], "分隔线长度必须跟随内容变化，不能是写死的固定值"
    assert rules[1] == _block_width(blocks[1]), "主机块前后的分隔线应等于主机行宽度"
    for length in rules:
        assert 20 <= length <= 119, f"分隔线长度越界：{length}"


def test_menu_is_colored_and_fits_the_terminal(app):
    with app.app_context():
        menu = _render_menu(
            [_entry(hostName="中文主机甲", accounts=[{"id": 1, "name": "中文账号"}])],
            "ops",
            "运维甲",
            "运维",
            "127.0.0.1",
            2,
            width=80,
        )
    assert "\x1b[" in menu, "菜单必须带颜色（用户要求：加点颜色突出重点信息）"
    for line in menu.split("\r\n"):
        assert display_width(line) <= 80, f"菜单里有超过终端宽度的行：{line!r}"


# ---------------------------------------------------------------- 字符画 ---
def test_banner_rows_keep_the_shield_and_wordmark_columns_aligned():
    rows = _banner_rows()
    width = [display_width(row) for row in rows]
    shield_width = max(display_width(row.rstrip()) for row in BANNER_SHIELD)
    wordmark_width = max(display_width(row.rstrip()) for row in BANNER_WORDMARK)
    assert len(rows) == len(BANNER_SHIELD)
    middle = width[BANNER_WORDMARK_OFFSET : BANNER_WORDMARK_OFFSET + len(BANNER_WORDMARK)]
    assert len(set(middle)) == 1, f"字标各行不等宽：{middle}"
    assert width[0] == shield_width, "首行只有盾牌，宽度应等于盾牌图形宽度"
    assert middle[0] == width[0] + display_width(BANNER_GAP) + wordmark_width
    for row, shield_row in zip(rows, BANNER_SHIELD, strict=True):
        assert strip_ansi(row).startswith(pad_display(shield_row.rstrip(), shield_width))
        assert "\x1b[" in row, "字符画必须带颜色"


def test_banner_is_centered_and_fits_the_terminal():
    banner = _render_banner(120)
    lines = [line for line in banner.split("\r\n") if strip_ansi(line).strip()]
    art = [line for line in lines if "统一运维入口" not in strip_ansi(line)]
    subtitle = [line for line in lines if "统一运维入口" in strip_ansi(line)]
    assert len(art) == len(BANNER_SHIELD)
    assert len(subtitle) == 1
    # 盾牌图形自带形状缩进（顶边比躯干窄），所以只能断言「整块共享同一缩进 + 整块居中」
    art_width = max(display_width(row) for row in _banner_rows())
    indent = (120 - art_width) // 2
    for line in art:
        plain = strip_ansi(line)
        assert plain.startswith(" " * indent), f"字符画各行必须左对齐到同一列：{plain!r}"
        assert plain[:indent].strip() == ""
    center = indent + art_width / 2
    assert abs(center - 120 / 2) <= 2, f"字符画没有居中：{center}"
    for line in lines:
        assert display_width(line) <= 120


def test_banner_takes_no_space_when_the_terminal_is_too_narrow():
    """窄终端宁可不出图，也绝不能折行 —— 折行的字符画比没有更难看。"""
    art_width = max(display_width(row) for row in _banner_rows())
    assert _render_banner(art_width + 5) == ""
    assert _render_banner(art_width + 6) != ""
    # 字符画够窄：60 列的小终端也能看到完整图形
    assert _render_banner(60) != ""
