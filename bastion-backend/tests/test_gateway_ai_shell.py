"""SSH 网关里的 ``/ask-ai``（需求 m08824 第 8 条）守卫。

覆盖三类真实风险：

1. **拦截不能改变 Shell 手感**：``/ask-ai`` 那一行必须*不转发回车*（否则目标机会
   把它当命令执行，``bash: /ask-ai: No such file or directory``），而普通命令
   （含退格、方向键、Ctrl-U）要一个字节都不少地转发。影子行缓冲里
   ``Ctrl-C/Ctrl-D/Ctrl-U/ESC`` 一律清空——宁可漏判（用户再敲一次）也不误判。
   分流逻辑已收到 ``app.ai.line_split.LineShadow``（与网页终端共用一份）；
   **粘贴**（bracketed paste，``ESC[200~`` / ``ESC[201~``）必须命中——旧实现把转义
   序列长度硬编码成 2 字节，会把 ``00~`` 当正文写进影子行导致漏检，就是用户报的 bug。
2. **终端渲染**：Markdown 流式重绘用的是 ``\\r\\x1b[K``（原地擦除重画），
   `````ai-card`` 卡片块**整块丢弃**（不打印 JSON 花括号），正文结束后再用
   ``render_cards_text`` 降级成 ASCII——所以断言里绝不能出现 ``{``。
3. **敏感操作**：Shell 里没有可交互控件，但「管理员账号 + 密码（掩码）→ 批准」
   必须写进 ``AiToolCall.status/confirmed_by`` **并落审计事件**；输错口令或直接回车
   必须转成 ``rejected``，不能因为"在终端里"就绕开审批。
"""

from __future__ import annotations

import pytest

from app.ai.line_split import AI_PREFIXES, LineShadow
from app.ai.service import Caller  # noqa: F401  (类型说明用，保持导入路径可见)
from app.extensions import db
from app.gateway import ai_shell
from app.gateway.ai_shell import (
    TerminalMarkdown,
    extract_question,
    is_ai_command,
    render_cards_text,
    render_inline,
    run_ask_ai,
)
from app.gateway.server import _split_ai_command, display_width, strip_ansi
from app.models import AiConversation, AiToolCall, AuditLog, User

QUOTES = "磁盘满了吗"


class FakeWriter:
    """收集网关写给通道的全部字节（含控制序列，便于断言重绘）。"""

    def __init__(self):
        self.text = ""

    def write(self, text: str):
        self.text += text

    def line(self, text: str = ""):
        self.write(text + "\r\n")

    @property
    def plain(self) -> str:
        return strip_ansi(self.text)


# ---------------------------------------------------------------------------
# 命令识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"/ask-ai {QUOTES}", QUOTES),
        (f"/ask {QUOTES}", QUOTES),
        (f"  /ask-ai   {QUOTES}  ", QUOTES),  # 前后空白不影响识别
        ("/ask-ai", ""),  # 只敲命令 → 空串（调用方打印用法）
        ("/ask", ""),
        ("/ask-ai\t列表一下主机", "列表一下主机"),
    ],
)
def test_extract_question_reads_the_question(line, expected):
    assert extract_question(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "ls -la",
        "echo /ask-ai hello",  # 不是行首命令
        "/ask-aiX 你好",  # 前缀粘连，不是本命令
        "/ask_ai 你好",
        "",
        "   ",
    ],
)
def test_extract_question_ignores_lookalikes(line):
    assert extract_question(line) is None
    assert is_ai_command(line) is False


def test_is_ai_command_accepts_both_prefixes():
    assert is_ai_command("/ask-ai 你好") is True
    assert is_ai_command("/ask 你好") is True


# ---------------------------------------------------------------------------
# 按键分流：普通命令零改动，/ask-ai 吞掉回车
# ---------------------------------------------------------------------------
def test_split_ai_command_forwards_plain_commands_byte_for_byte():
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(b"ls -la\r", shadow)
    assert forward == b"ls -la\r"
    assert ai_line is None
    assert shadow.pending == ""  # 回车后影子行清空


def test_split_ai_command_swallows_the_enter_for_ask_ai():
    shadow = LineShadow()
    raw = f"/ask-ai {QUOTES}\r".encode()
    forward, ai_line = _split_ai_command(raw, shadow)
    assert ai_line == f"/ask-ai {QUOTES}"
    assert b"\r" not in forward and b"\n" not in forward, "回车不能转发，否则目标机会执行它"
    assert forward == f"/ask-ai {QUOTES}".encode()


def test_split_ai_command_accumulates_a_line_across_chunks():
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(b"/ask-ai hel", shadow)
    assert ai_line is None and forward == b"/ask-ai hel"
    forward, ai_line = _split_ai_command(b"lo\r", shadow)
    assert ai_line == "/ask-ai hello"


def test_split_ai_command_applies_backspace_to_the_shadow_line():
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(b"/ask-ax\x7f", shadow)
    assert ai_line is None
    assert forward == b"/ask-ax\x7f"  # 退格照样转发给目标机
    _, ai_line = _split_ai_command(b"i hi\r", shadow)
    assert ai_line == "/ask-ai hi"


@pytest.mark.parametrize("reset", [b"\x15", b"\x03", b"\x04", b"\x1b[D"])
def test_split_ai_command_drops_the_shadow_line_on_control_keys(reset):
    """Ctrl-U / Ctrl-C / Ctrl-D / 方向键之后，影子行不再可信 → 绝不误判。"""
    shadow = LineShadow()
    _split_ai_command(b"/ask-ai " + reset, shadow)
    _, ai_line = _split_ai_command(f"{QUOTES}\r".encode(), shadow)
    assert ai_line is None


# ---------------------------------------------------------------------------
# 粘贴加固（用户报的 bug：粘贴的 /ask-ai 漏检后被目标机 bash 执行）
# ---------------------------------------------------------------------------
def test_split_ai_command_hits_on_bracketed_paste():
    """``ESC[200~/ask-ai 你好\\rESC[201~`` 必须命中。

    旧实现遇 ESC 只硬编码吞 2 个字节（``escape = 2``），``ESC[200~`` 是 5 字节，
    于是 ``00~`` 被当正文写进影子行、``/ask-ai`` 漏检并随回车转发给目标机，
    用户看到 ``-bash: /ask-ai: 没有那个文件或目录``。粘贴标记本身照旧转发。
    """
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(b"\x1b[200~/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd\r\x1b[201~", shadow)
    assert ai_line == "/ask-ai 你好", "粘贴的 /ask-ai 必须被识别（旧实现会漏检）"
    assert b"\r" not in forward
    assert b"\x1b[200~" in forward, "粘贴开始标记照旧转发给目标机"
    # 命中点之后同一批到的字节按冻结语义被丢弃（结束标记本身无害：这一行已经被抹掉）。
    # 结束标记若是单独一批到达，仍会原样转发：
    tail, hit2 = _split_ai_command(b"\x1b[201~", shadow)
    assert hit2 is None and tail == b"\x1b[201~"


def test_split_ai_command_keeps_escape_state_across_chunks():
    """转义序列被 TCP 切碎（``ESC[20`` + ``0~/ask-ai hi\\r``）也要命中。"""
    shadow = LineShadow()
    out1, hit1 = _split_ai_command(b"\x1b[20", shadow)
    out2, hit2 = _split_ai_command(b"0~/ask-ai hi\r", shadow)
    assert hit1 is None and hit2 == "/ask-ai hi"
    assert bytes(out1) + out2 == b"\x1b[200~/ask-ai hi"


@pytest.mark.parametrize("sequence", [b"\x1b[A", b"\x1b[1;5C", b"\x1bOP"])
def test_split_ai_command_escape_keys_do_not_pollute_the_shadow(sequence):
    """方向键 / 功能键只转发，不进影子行。"""
    shadow = LineShadow()
    forward, hit = _split_ai_command(sequence, shadow)
    assert hit is None and sequence in forward
    assert shadow.pending == ""
    _, hit = _split_ai_command(b"/ask-ai hi\r", shadow)
    assert hit == "/ask-ai hi"


def test_split_ai_command_bare_prefix_hits_with_empty_question():
    """只敲 ``/ask-ai`` 也要命中（第二项是命中的命令行，问题文本由 extract_question 给空串）。"""
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(b"/ask-ai\r", shadow)
    assert ai_line == "/ask-ai"
    assert extract_question(ai_line) == ""  # 空问题 → 调用方打印用法
    assert b"\r" not in forward


@pytest.mark.parametrize("raw", [b"/ask-aiX\r", b"echo /ask-ai\r"])
def test_split_ai_command_lookalikes_are_forwarded_verbatim(raw):
    shadow = LineShadow()
    forward, ai_line = _split_ai_command(raw, shadow)
    assert ai_line is None
    assert forward == raw, "不是 AI 命令的输入必须原样转发（含回车）"


def test_gateway_reuses_the_single_source_of_truth():
    """网关侧不再自己写一份前缀/识别逻辑（收敛成 ``app/ai/line_split.py`` 一份真源）。"""
    from app.ai import line_split

    assert ai_shell.AI_PREFIXES is line_split.AI_PREFIXES is AI_PREFIXES
    assert ai_shell.extract_question is line_split.extract_question
    assert ai_shell.is_ai_command is line_split.is_ai_command


# ---------------------------------------------------------------------------
# Markdown 流式渲染
# ---------------------------------------------------------------------------
def _feed(chunks, width=80):
    writer = FakeWriter()
    md = TerminalMarkdown(writer, width)
    for chunk in chunks:
        md.feed(chunk)
    md.flush()
    return writer


def test_terminal_markdown_styles_headings_bullets_and_code():
    writer = _feed(["## 磁盘\n", "- / 使用 **91%**\n", "\n", "```bash\n", "df -h\n", "```\n"])
    plain = writer.plain
    assert "磁盘" in plain
    assert "• / 使用 91%" in plain
    assert "df -h" in plain
    assert "\x1b[" in writer.text, "必须有颜色（SGR）序列"


def test_terminal_markdown_redraws_the_partial_line_in_place():
    """流式渲染靠 ``\\r`` + ``\\x1b[K`` 原地擦除重画，而不是每来一个字换一行。"""
    writer = _feed(["磁盘", "使用率", " 91%", "\n"])
    assert "\r\x1b[K" in writer.text
    assert writer.plain.count("磁盘使用率 91%") >= 1


def test_terminal_markdown_drops_ai_card_blocks_entirely():
    writer = _feed(['结论如下：\n', "```ai-card\n", '{"type":"table","rows":[[1]]}\n', "```\n", "以上。\n"])
    plain = writer.plain
    assert "结论如下" in plain and "以上" in plain
    assert "{" not in plain and "rows" not in plain, "终端不打印卡片 JSON"


def test_terminal_markdown_never_leaks_card_json_while_streaming():
    """真机缺陷回归：逐字符流式时，未完成的围栏会先被"预览"一次、收到换行再落盘
    一次；两次都翻状态就会把 ai-card 判定翻掉，卡片 JSON 整段漏到终端上。"""
    payload = '好的\n\n```ai-card\n{"type": "table", "rows": [[1]]}\n```\n\n结束\n'
    writer = FakeWriter()
    markdown = TerminalMarkdown(writer, width=120)
    for char in payload:
        markdown.feed(char)
    markdown.flush()
    plain = writer.plain
    assert "好的" in plain and "结束" in plain
    assert '"type"' not in plain and "rows" not in plain, "卡片 JSON 不能出现在终端"


def test_terminal_markdown_streaming_does_not_double_toggle_code_fences():
    """同一个 ``` 围栏不能被"预览"和"落盘"各翻一次，否则块内块外的样式会反过来。"""
    payload = "前\n```bash\ndf -h\n```\n后\n"
    writer = FakeWriter()
    markdown = TerminalMarkdown(writer, width=120)
    for char in payload:
        markdown.feed(char)
    markdown.flush()
    assert markdown._in_code is False, "围栏收全后必须回到正文状态"
    assert "后" in writer.plain


def test_terminal_markdown_flushes_overlong_lines_without_redrawing():
    writer = _feed(["x" * 200 + "\n"], width=60)
    assert writer.plain.count("x" * 200) == 1
    assert not any(
        line.startswith("\r\x1b[K") and len(line) > 60
        for line in writer.text.split("\r\n")
    )


def test_render_inline_keeps_text_and_marks_styles():
    rendered = render_inline("看 `df -h` 和 **详情**")
    assert strip_ansi(rendered) == "看 df -h 和 详情"
    assert rendered != strip_ansi(rendered)


# ---------------------------------------------------------------------------
# 卡片降级成终端文本
# ---------------------------------------------------------------------------
def test_render_cards_text_covers_all_four_card_types():
    cards = [
        {"type": "table", "title": "在线会话", "columns": [{"key": "user", "title": "用户"}, {"key": "n", "title": "会话数"}], "rows": [{"user": "admin", "n": 2}, {"user": "运维员", "n": 1}]},
        {"type": "keyvalue", "title": "主机状态", "items": [{"label": "CPU", "value": "12%", "status": "success"}, {"label": "内存", "value": "88%", "status": "warning"}]},
        {"type": "alert", "level": "error", "title": "风险", "text": "检测到高危命令"},
        {"type": "steps", "title": "执行步骤", "items": [{"title": "连接", "status": "finish"}, {"title": "执行", "status": "process"}]},
    ]
    lines = render_cards_text(cards, 80)
    plain = "\n".join(strip_ansi(line) for line in lines)
    assert "在线会话" in plain and "admin" in plain and "运维员" in plain
    assert "CPU：12%" in plain and "88%" in plain
    assert "[error] 检测到高危命令" in plain
    assert "✔ 连接" in plain and "○ 执行" in plain
    assert "{" not in plain and "[" not in plain.replace("[error]", "")


def test_render_cards_text_aligns_cjk_table_columns():
    cards = [
        {
            "type": "table",
            "columns": [{"key": "a", "title": "主机"}, {"key": "b", "title": "状态"}],
            "rows": [{"a": "db-01", "b": "正常"}, {"a": "数据库-02", "b": "异常"}],
        }
    ]
    lines = [line for line in render_cards_text(cards, 80) if line.strip()]
    rows = [strip_ansi(line) for line in lines if "db-01" in line or "数据库-02" in line]
    assert len(rows) == 2, "表格行没渲染出来"
    # 中文占两列 ⇒ 每行补齐后的可见宽度必须一致（否则"数据库-02"这一行会被顶歪）
    assert len({display_width(row) for row in rows}) == 1
    assert rows[0].startswith("db-01") and rows[1].startswith("数据库-02")
    # 注意：列宽是"显示列"而不是"字符个数"，所以要用 display_width 比较前缀宽度
    heads = [row[: row.index(second)] for row, second in zip(rows, ("正常", "异常"))]
    assert len({display_width(head) for head in heads}) == 1, "两行的第二列没有左对齐"


# ---------------------------------------------------------------------------
# 一个完整回合（用替身客户端，不联网）
# ---------------------------------------------------------------------------
@pytest.fixture()
def ai_enabled(app):
    app.config["AI_ENABLED"] = True
    app.config["DEEPSEEK_API_KEY"] = "test-key"
    return app


def _user_id(app, username="admin"):
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        assert user is not None
        return user.id


def _stub_client(monkeypatch):
    monkeypatch.setattr(ai_shell, "create_client", lambda config: object())


def test_run_ask_ai_prints_usage_for_an_empty_question(ai_enabled):
    writer = FakeWriter()
    assert run_ask_ai(
        ai_enabled, writer, user_id=_user_id(ai_enabled), entry=None, account=None,
        sid="sid-1", question="", width=100, read_line=lambda *a, **k: ("", True),
    ) is None
    assert "/ask-ai <问题>" in writer.plain


def test_run_ask_ai_refuses_when_the_key_is_missing(app, make_user, monkeypatch):
    app.config["AI_ENABLED"] = True
    app.config["DEEPSEEK_API_KEY"] = ""
    writer = FakeWriter()
    run_ask_ai(
        app, writer, user_id=_user_id(app), entry=None, account=None,
        sid="sid-1", question=QUOTES, width=100, read_line=lambda *a, **k: ("", True),
    )
    assert "DEEPSEEK_API_KEY" in writer.plain
    with app.app_context():
        assert AiConversation.query.count() == 0


def test_run_ask_ai_refuses_without_the_ai_use_permission(ai_enabled, make_user):
    viewer_id = make_user("ai-none", role_code="viewer")
    writer = FakeWriter()
    run_ask_ai(
        ai_enabled, writer, user_id=viewer_id, entry=None, account=None,
        sid="sid-1", question=QUOTES, width=100, read_line=lambda *a, **k: ("", True),
    )
    assert "ai:use" in writer.plain
    with ai_enabled.app_context():
        assert AiConversation.query.count() == 0


def test_run_ask_ai_streams_markdown_tool_calls_and_cards(ai_enabled, monkeypatch):
    _stub_client(monkeypatch)

    def fake_run_turn(app_, conversation, caller, client, *, user_text="", resume=False):
        assert user_text == QUOTES
        yield {"type": "start", "conversationId": conversation.id, "model": "deepseek-flash"}
        yield {"type": "content", "delta": "## 磁盘\n"}
        yield {"type": "content", "delta": "- / 已用 **91%**\n"}
        yield {"type": "tool_call", "callId": "c1", "name": "list_hosts", "args": {"page": 1}, "sensitive": False}
        yield {"type": "tool_result", "callId": "c1", "name": "list_hosts", "ok": True, "summary": "共 4 台主机"}
        yield {"type": "cards", "cards": [{"type": "alert", "level": "warning", "text": "建议清理 /var/log"}]}
        yield {"type": "message_end", "status": "done"}

    monkeypatch.setattr("app.ai.service.run_turn", fake_run_turn)

    writer = FakeWriter()
    conversation_id = run_ask_ai(
        ai_enabled, writer, user_id=_user_id(ai_enabled),
        entry={"hostId": 7, "hostName": "db-01", "address": "10.0.0.12"},
        account={"username": "root"}, sid="sid-9", question=QUOTES, width=100,
        read_line=lambda *a, **k: ("", True),
    )
    plain = writer.plain
    assert "磁盘" in plain and "• / 已用 91%" in plain
    assert "→ 调用工具 list_hosts" in plain
    assert "← 工具结果 list_hosts 成功：共 4 台主机" in plain
    assert "[warning] 建议清理 /var/log" in plain
    # 工具入参允许以紧凑 JSON 提示（人要看得见 AI 在调什么），但卡片本身绝不能漏 JSON
    assert '"type"' not in plain and '"columns"' not in plain and '"rows"' not in plain
    assert "db-01" in plain and "root" in plain  # 上下文行

    with ai_enabled.app_context():
        row = db.session.get(AiConversation, conversation_id)
        assert row is not None
        assert row.source == "shell", "Shell 里的对话必须标记来源，审计才追得回来"
        assert row.title == QUOTES
        assert row.host_name == "db-01"
        assert row.sid == "sid-9"


def _pending_row(app, conversation_id, caller, tool="terminal_exec"):
    row = AiToolCall(
        conversation_id=conversation_id,
        call_id="call_1",
        user_id=caller.id,
        username=caller.username,
        tool_name=tool,
        arguments={"hostId": 7, "command": "rm -rf /tmp/x"},
        required_permission="ai:tool_exec",
        sensitive=True,
        status="pending",
    )
    db.session.add(row)
    db.session.commit()
    return row.id


def _confirming_turn(app, store):
    """替身 run_turn：第一轮制造待确认的敏感操作，resume 后收尾。"""

    def fake_run_turn(app_, conversation, caller, client, *, user_text="", resume=False):
        if resume:
            yield {"type": "content", "delta": "已执行完毕。\n"}
            yield {"type": "message_end", "status": "done"}
            return
        with app_.app_context():
            row_id = _pending_row(app_, conversation.id, caller)
        store["row_id"] = row_id
        yield {
            "type": "tool_call", "callId": "call_1", "name": "terminal_exec",
            "args": {"hostId": 7}, "sensitive": True, "permission": "ai:tool_exec",
        }
        yield {
            "type": "confirm_required", "conversationId": conversation.id, "count": 1,
            "toolCallId": row_id, "callId": "call_1", "name": "terminal_exec",
            "toolCalls": [{"id": row_id, "callId": "call_1", "name": "terminal_exec", "args": {"hostId": 7}, "permission": "ai:tool_exec"}],
            "reason": "该操作会修改堡垒机配置或在目标机上执行动作，请输入管理员账号密码确认",
        }

    return fake_run_turn


def _asking_reader(answers, seen):
    def read_line(prompt, mask=False):
        seen.append((prompt, mask))
        return answers.pop(0), False

    return read_line


def _pending_fixture(app, monkeypatch, answers):
    _stub_client(monkeypatch)
    store: dict = {"row_id": None}
    monkeypatch.setattr("app.ai.service.run_turn", _confirming_turn(app, store))
    seen: list = []
    writer = FakeWriter()
    conversation_id = run_ask_ai(
        app, writer, user_id=_user_id(app), entry=None, account=None, sid="sid-1",
        question=QUOTES, width=100, read_line=_asking_reader(answers, seen),
    )
    return writer, seen, store, conversation_id


def test_sensitive_operation_asks_admin_password_then_executes(ai_enabled, monkeypatch):
    writer, seen, store, _ = _pending_fixture(ai_enabled, monkeypatch, ["admin", "admin123"])
    assert [item[0] for item in seen] == ["管理员账号：", "管理员密码："]
    assert seen[1][1] is True, "密码必须掩码读入"
    assert "已确认，继续执行" in writer.plain
    assert "已执行完毕" in writer.plain
    with ai_enabled.app_context():
        row = db.session.get(AiToolCall, store["row_id"])
        assert row.status == "approved"
        assert row.confirmed_by == "admin"
        assert row.confirmed_at is not None
        audit = AuditLog.query.filter_by(action="ai_tool_confirm").first()
        assert audit is not None and audit.actor_username == "admin"


def test_sensitive_operation_rejects_a_wrong_admin_password(ai_enabled, monkeypatch):
    writer, _, store, _ = _pending_fixture(ai_enabled, monkeypatch, ["admin", "not-the-password"])
    assert "管理员账号或密码错误" in writer.plain
    assert "已执行完毕" not in writer.plain, "口令错误绝不能继续执行"
    with ai_enabled.app_context():
        assert db.session.get(AiToolCall, store["row_id"]).status == "rejected"


def test_sensitive_operation_rejects_on_empty_username(ai_enabled, monkeypatch):
    """直接回车 = 拒绝（终端里没有按钮，回车就是"取消"）。"""
    writer, seen, store, _ = _pending_fixture(ai_enabled, monkeypatch, [""])
    assert len(seen) == 1, "拒绝时不该再问密码"
    assert "已拒绝该操作" in writer.plain
    with ai_enabled.app_context():
        assert db.session.get(AiToolCall, store["row_id"]).status == "rejected"
