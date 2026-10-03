"""SSH 网关会话里的 ``/ask-ai``：把 AI 运维搬进 Shell。

需求原文（m08824）：**ssh 里面链接到一台机器后，可以随时输入 ``/ask-ai <问题>`` 问 AI，
但不输出可交互控件，但是 Shell 里 Markdown 要流式渲染**。

三个设计决定，写在这里免得以后被"优化"掉：

1. **拦截不改变 Shell 手感**。用户在会话里敲的每一个字节依旧原样转发给目标机，
   所以本地回显、Tab 补全、Ctrl-C 全是目标机 bash/readline 的行为——网关另存一份
   "影子行缓冲"，只用来判断这一行是不是 ``/ask-ai``。当按下回车且影子行命中时，
   网关向目标机补发一个 ``Ctrl-U``（readline 的 unix-line-discard，只清行不执行），
   把这一行从 bash 里抹掉，然后交给 AI。**普通命令完全不受影响**。
2. **不输出可交互控件**。AI 正文里的卡片块（`````ai-card``）在终端不打印 JSON，
   而是在正文结束后用 ``render_cards_text`` 降级成 ASCII 文本；敏感操作确认走
   "管理员账号 / 密码（掩码）"两步文本提示——没有控件，但审批人、审批时间、
   审计事件与网页端**完全一致**（同一个 ``approve_tool_call`` / ``reject_tool_call``）。
3. **复用同一套权限与审计**。AI 的工具会带着调用者的身份去请求本项目自己的 REST API；
   网关里没有浏览器 JWT，所以为本次会话在**内存里临时签发**一个短时令牌
   （2 小时、不落盘、不外发、只用于 ``app.test_client()`` 的同机请求），
   这样 AI 在 Shell 里能做的事**永远不会超过这个人在网页端能做的事**。

流式渲染见 :class:`TerminalMarkdown`：已完成的整行直接落盘，末尾未完成的一行用
``\\r`` + ``\\x1b[K`` 原地重绘（超出终端宽度就落盘，避免折行残影）。
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Callable

from ..ai import service as ai_service
from ..ai.client import AiError, create_client
# 命令前缀与识别的唯一真源，与网页终端共用一份（各写一份必然漂移：
# 网关那侧粘贴时的 bracketed paste 就是这么漏检的）。
from ..ai.line_split import AI_PREFIXES, extract_question, is_ai_command  # noqa: F401
from ..models import User
from ..security import has_permission, is_admin, verify_password
# 反向引用 server 的配色/宽度工具：server 只在会话循环里延迟导入本模块，不会形成循环。
from .server import (
    CLR_BOLD,
    CLR_CYAN,
    CLR_DIM,
    CLR_GREEN,
    CLR_RED,
    CLR_YELLOW,
    color,
    display_width,
    strip_ansi,
)

#: 首次命中时提示一次用法，之后只提示错误
AI_USAGE = "/ask-ai <问题>  问 AI（在会话里随时可用，AI 的输出会以 Markdown 流式渲染）"

#: 敏感操作在 Shell 里最多连续确认几轮（审批后 AI 可能又发起新的敏感操作）
MAX_CONFIRM_ROUNDS = 3


# ---------------------------------------------------------------------------
# Markdown → 终端
# ---------------------------------------------------------------------------
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_]\s*){3,}$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*```\s*(\S*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_CODE_SPAN = re.compile(r"`([^`]*)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def render_inline(text: str) -> str:
    """行内样式：粗体 / 行内代码 / 链接（Markdown 的嵌套组合不做完整支持，够用即可）。"""
    out = _BOLD.sub(lambda m: color(m.group(1), CLR_BOLD), text)
    out = _CODE_SPAN.sub(lambda m: color(m.group(1), CLR_CYAN), out)
    out = _LINK.sub(lambda m: m.group(1) + color(f" ({m.group(2)})", CLR_DIM), out)
    return out


class TerminalMarkdown:
    """把 Markdown 增量渲染到终端。

    * 整行（收到 ``\\n``）→ 渲染样式后落盘；
    * 末尾未完成的一行 → ``\\r\\x1b[K`` 原地重绘，所以字是"长出来"的；
    * ``\\`\\`\\`ai-card`` 块在终端不打印（正文结束后由 :func:`render_cards_text` 降级）；
    * 一行渲染后超过终端宽度就落盘而不重绘，避免折行后重绘留残影。
    """

    def __init__(self, writer, width: int = 80):
        self.writer = writer
        self.width = max(24, int(width or 80))
        self._buf = ""
        self._partial = False
        self._in_code = False
        self._in_card = False

    # -- 对外接口 ---------------------------------------------------------- #
    def feed(self, delta: str):
        if not delta:
            return
        self._buf += delta.replace("\r\n", "\n").replace("\r", "\n")
        while True:
            index = self._buf.find("\n")
            if index < 0:
                break
            line, self._buf = self._buf[:index], self._buf[index + 1 :]
            self._commit(line)
        self._draw()

    def flush(self):
        """正文结束：把残留的半行落盘（不要吞掉模型的最后一句）。"""
        tail, self._buf = self._buf, ""
        if tail:
            self._commit(tail)
        else:
            self._erase()

    def break_line(self):
        """工具调用之类的旁白要独立成行时调用（先收尾当前半行）。"""
        if self._buf:
            self.flush()
        self._erase()

    # -- 内部 ------------------------------------------------------------- #
    def _erase(self):
        if self._partial:
            self.writer.write("\r\x1b[K")
            self._partial = False

    def _commit(self, line: str):
        rendered = self._render_line(line)
        if rendered is None:  # 卡片块内部：整块丢弃
            self._erase()
            return
        self._erase()
        self.writer.write(rendered + "\r\n" if rendered else "\r\n")

    def _draw(self):
        if not self._buf or self._in_card:
            self._erase()
            return
        rendered = self._render_line(self._buf, partial=True)
        if rendered is None:
            self._erase()
            return
        if display_width(strip_ansi(rendered)) > self.width - 1:
            self.writer.write(rendered + "\r\n")
            self._partial = False
            self._buf = ""
            return
        self.writer.write("\r\x1b[K" + rendered)
        self._partial = True

    def _render_line(self, line: str, partial: bool = False) -> str | None:
        """返回渲染后的文本；``None`` 表示这一行不打印，``""`` 表示空行。

        ``partial=True`` 只做预览、**绝不改动状态**：流式下一个未完成的行会先被
        预览一次、等收到换行再落盘一次，如果两次都翻状态，``` 围栏和 ai-card
        就会被翻转两遍，卡片里的 JSON 会整段漏到终端上（真机踩过）。
        """
        if self._in_card:
            if partial:
                return None
            if line.strip().startswith("```"):
                self._in_card = False
            return None
        fence = _FENCE.match(line)
        if fence:
            if partial:
                return ""  # 围栏还没收全，先不显示，等落盘那次再定状态
            lang = (fence.group(1) or "").lower()
            if lang.startswith("ai-card"):
                self._in_card = True
                return None
            self._in_code = not self._in_code
            return color("┈" * min(self.width - 1, 48), CLR_DIM)
        if self._in_code:
            return color(line, CLR_DIM)
        if not line.strip():
            return ""
        heading = _HEADING.match(line)
        if heading:
            return color(render_inline(heading.group(2)), CLR_BOLD, CLR_CYAN)
        if _RULE.match(line):
            return color("─" * min(self.width - 1, 60), CLR_DIM)
        bullet = _BULLET.match(line)
        if bullet:
            return f"{bullet.group(1)}{color('•', CLR_YELLOW)} {render_inline(bullet.group(2))}"
        ordered = _ORDERED.match(line)
        if ordered:
            return (
                f"{ordered.group(1)}{color(ordered.group(2) + '.', CLR_YELLOW)} "
                f"{render_inline(ordered.group(3))}"
            )
        quote = _QUOTE.match(line)
        if quote:
            return color("│ ", CLR_DIM) + color(render_inline(quote.group(1)), CLR_DIM)
        if _TABLE_SEP.match(line) and "|" in line:
            return color("─" * min(self.width - 1, 60), CLR_DIM)
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            return color("│ ", CLR_DIM) + color(" │ ", CLR_DIM).join(
                render_inline(cell) for cell in cells
            )
        return render_inline(line)


# ---------------------------------------------------------------------------
# 卡片降级成文本
# ---------------------------------------------------------------------------
def _ascii_table(headers: list[str], rows: list[list[str]], width: int) -> list[str]:
    """把表格卡片渲染成定宽文本表（按可见宽度对齐，CJK 也不会歪）。"""
    if not headers and not rows:
        return []
    columns = max(len(headers), max((len(row) for row in rows), default=0))
    headers = headers + [""] * (columns - len(headers))
    cells = [list(row) + [""] * (columns - len(row)) for row in rows]
    widths = [display_width(strip_ansi(headers[i])) for i in range(columns)]
    for row in cells:
        for i in range(columns):
            widths[i] = max(widths[i], display_width(strip_ansi(row[i])))
    budget = max(12, width - 3 * (columns - 1) - 2)
    total = sum(widths) or 1
    if total > budget:  # 超宽就按比例压缩每一列（最后一列吃掉余量）
        widths = [max(6, int(w * budget / total)) for w in widths]
        overflow = sum(widths) - budget
        if overflow > 0:
            widths[-1] = max(6, widths[-1] - overflow)

    def row_text(values: list[str], bold: bool = False) -> str:
        parts: list[str] = []
        for index in range(columns):
            text = str(values[index])
            while display_width(strip_ansi(text)) > widths[index] and len(text) > 1:
                text = text[:-1]
            pad = " " * max(0, widths[index] - display_width(strip_ansi(text)))
            parts.append((color(text, CLR_BOLD) if bold else text) + pad)
        return color("  ", CLR_DIM).join(parts)

    lines = [row_text(headers, bold=True)]
    lines.append(color("─" * min(max(8, width - 1), sum(widths) + 2 * columns), CLR_DIM))
    lines.extend(row_text(row) for row in cells)
    return lines


def render_cards_text(cards: list[dict], width: int = 80) -> list[str]:
    """把 ``cards`` 事件里的结构化卡片降级成终端文本（不打印任何 JSON 花括号）。"""
    lines: list[str] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        ctype = str(card.get("type") or "").lower()
        title = str(card.get("title") or "").strip()
        if title:
            lines.append(color(f"▌ {title}", CLR_BOLD, CLR_CYAN))
        if ctype == "table":
            headers = [
                str(col.get("title") or col.get("key") or "")
                for col in (card.get("columns") or [])
                if isinstance(col, dict)
            ]
            keys = [
                str(col.get("key") or col.get("title") or "")
                for col in (card.get("columns") or [])
                if isinstance(col, dict)
            ]
            rows: list[list[str]] = []
            for row in card.get("rows") or []:
                if isinstance(row, dict):
                    rows.append([_cell(row.get(key)) for key in keys])
                elif isinstance(row, (list, tuple)):
                    rows.append([_cell(item) for item in row])
            lines.extend(_ascii_table(headers, rows, width))
        elif ctype == "keyvalue":
            for item in card.get("items") or []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "")
                value = _cell(item.get("value"))
                status = str(item.get("status") or "")
                mark = color("●", CLR_GREEN if status in ("success", "ok") else CLR_YELLOW) if status else "•"
                lines.append(f"  {mark} {label}：{color(value, CLR_BOLD)}")
        elif ctype == "alert":
            level = str(card.get("level") or "info")
            palette = {
                "error": CLR_RED,
                "warning": CLR_YELLOW,
                "success": CLR_GREEN,
                "info": CLR_CYAN,
            }
            tint = palette.get(level, CLR_CYAN)
            text = str(card.get("text") or "").replace("\n", " ")
            lines.append(color(f"[{level}] {text}", tint))
        elif ctype == "steps":
            for index, item in enumerate(card.get("items") or [], start=1):
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "")
                mark = {"finish": "✔", "done": "✔", "success": "✔", "error": "✘"}.get(status, "○")
                lines.append(
                    f"  {color(mark, CLR_GREEN if mark == '✔' else CLR_YELLOW)} "
                    f"{item.get('title') or ''}"
                    + (color(f"  {item.get('description')}", CLR_DIM) if item.get("description") else "")
                )
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# 执行一个 /ask-ai 回合
# ---------------------------------------------------------------------------
def _mint_token(user: User, *, hours: int = 2) -> str:
    """给本次 Shell 会话临时签发 JWT（只在内存里用于同机 REST 调用）。"""
    from flask_jwt_extended import create_access_token

    return create_access_token(
        identity=str(user.id),
        additional_claims={
            "username": user.username,
            "role": user.role_code,
            "permissions": sorted(user.permission_set()),
            "source": "ssh-gateway",
        },
        expires_delta=timedelta(hours=hours),
    )


def _entry_host(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {
        "id": entry.get("hostId"),
        "name": entry.get("hostName") or "",
        "address": entry.get("address") or "",
    }


def run_ask_ai(
    app,
    writer,
    *,
    user_id: int,
    entry: dict | None,
    account: dict | None,
    sid: str,
    question: str,
    width: int,
    read_line: Callable[..., tuple[str, bool]],
    conversation_id: int | None = None,
) -> int | None:
    """执行一次 ``/ask-ai``；返回对话 ID（下次接着问同一个话题）。

    ``read_line(prompt, mask)`` 由网关提供（复用登录用的逐字符读行）。
    这里只接收 ``user_id``：网关线程里没有请求上下文，所有 ORM 读取都在
    自己的 app context 内完成，避免 DetachedInstanceError。
    """
    if not question:
        writer.line(color(AI_USAGE, CLR_YELLOW))
        return conversation_id

    if not bool(app.config.get("AI_ENABLED")):
        writer.line(color("[堡垒机] AI 运维已关闭（.env 里 AI_ENABLED=0）", CLR_RED))
        return conversation_id
    if not str(app.config.get("DEEPSEEK_API_KEY") or "").strip():
        writer.line(color("[堡垒机] 尚未配置 DEEPSEEK_API_KEY，请写入 bastion-backend/.env 后重启服务", CLR_RED))
        return conversation_id

    from ..extensions import db

    with app.app_context():
        user = db.session.get(User, user_id)
        if user is None or not user.is_active() or user.is_locked():
            writer.line(color("[堡垒机] 当前账号不可用，无法使用 AI 运维", CLR_RED))
            return conversation_id
        if not has_permission(user, "ai:use"):
            writer.line(color("[堡垒机] 当前账号没有「AI 对话」权限（ai:use），请联系管理员授权", CLR_RED))
            return conversation_id

        host = _entry_host(entry)
        caller = ai_service.Caller.from_user(
            user,
            token=_mint_token(user),
            source="shell",
            user_agent="ssh-gateway",
        )

        conversation = None
        if conversation_id:
            from ..models import AiConversation

            conversation = db.session.get(AiConversation, conversation_id)
            if conversation is not None and conversation.user_id != user.id:
                conversation = None
        if conversation is None:
            conversation = ai_service.create_conversation(
                caller,
                title=question[:60],
                source="shell",
                model=app.config.get("AI_MODEL", ""),
                host=host,
                sid=sid,
            )

        try:
            client = create_client(app.config)
        except AiError as exc:
            writer.line(color(f"[堡垒机] AI 客户端初始化失败：{exc}", CLR_RED))
            return conversation.id

        where = (host or {}).get("name") or "-"
        writer.line(
            color("[堡垒机] AI 上下文：", CLR_DIM)
            + f"{where} · 账号 {(account or {}).get('username') or '-'} · 会话 {sid or '-'}"
        )
        writer.line(color(f"[堡垒机] 提问：{question}", CLR_CYAN))
        writer.line()

        try:
            _turn(
                app,
                writer,
                conversation=conversation,
                caller=caller,
                client=client,
                width=width,
                read_line=read_line,
                user_text=question,
            )
        except AiError as exc:
            writer.line(color(f"[堡垒机] AI 调用失败：{exc}", CLR_RED))
        except Exception as exc:  # noqa: BLE001 - Shell 里必须给可读原因
            app.logger.exception("gateway /ask-ai failed")
            writer.line(color(f"[堡垒机] AI 执行失败：{exc}", CLR_RED))
        return conversation.id


def _turn(
    app,
    writer,
    *,
    conversation,
    caller,
    client,
    width: int,
    read_line,
    user_text: str,
    resume: bool = False,
):
    """跑一个回合；遇到敏感操作就地做管理员确认，最多 :data:`MAX_CONFIRM_ROUNDS` 轮。"""
    rounds = 0
    while True:
        markdown = TerminalMarkdown(writer, width)
        cards: list[dict] = []
        pending_rows: list = []
        confirm_event: dict | None = None
        reasoning = ""
        failed = False

        for event in ai_service.run_turn(
            app,
            conversation,
            caller,
            client,
            user_text=user_text,
            resume=resume,
        ):
            kind = event.get("type")
            if kind == "content":
                markdown.feed(str(event.get("delta") or ""))
            elif kind == "reasoning":
                reasoning += str(event.get("delta") or "")
            elif kind == "tool_call":
                markdown.break_line()
                name = event.get("name") or event.get("tool") or "工具"
                flag = color(" [敏感操作]", CLR_YELLOW) if event.get("sensitive") else ""
                writer.line(color("→ 调用工具 ", CLR_DIM) + color(str(name), CLR_CYAN) + flag)
                args = event.get("args")
                if args:
                    writer.line(color(f"  参数：{_compact(args)}", CLR_DIM))
            elif kind == "tool_result":
                markdown.break_line()
                name = event.get("name") or event.get("tool") or "工具"
                ok = bool(event.get("ok", event.get("status") in ("success", "ok")))
                tint = CLR_GREEN if ok else CLR_RED
                writer.line(
                    color("← 工具结果 ", CLR_DIM)
                    + color(str(name), CLR_CYAN)
                    + color(" 成功" if ok else " 失败", tint)
                    + color(f"：{event.get('summary') or ''}", CLR_DIM)
                )
            elif kind == "confirm_required":
                confirm_event = event
            elif kind == "cards":
                cards = list(event.get("cards") or [])
            elif kind == "message_end":
                markdown.flush()
                if not cards:
                    cards = list(event.get("cards") or [])
            elif kind == "error":
                failed = True
                markdown.break_line()
                writer.line(color(f"[堡垒机] AI 出错：{event.get('message') or '未知错误'}", CLR_RED))

        markdown.flush()
        writer.line()

        if reasoning.strip():
            writer.line(color("（思考过程）" + _compact(reasoning.strip(), 240), CLR_DIM))
            writer.line()

        if cards:
            for line in render_cards_text(cards, width):
                writer.line(line)
            writer.line()

        if failed or confirm_event is None:
            return
        rounds += 1
        if rounds > MAX_CONFIRM_ROUNDS:
            writer.line(color("[堡垒机] 敏感操作确认次数过多，已在网页端保留待确认记录", CLR_YELLOW))
            return

        pending_rows = ai_service.pending_tool_calls(conversation)
        if not pending_rows:
            return

        writer.line()
        writer.line(
            color(f"[堡垒机] AI 请求执行 {len(pending_rows)} 个敏感操作，需要管理员确认：", CLR_YELLOW)
        )
        for row in pending_rows:
            writer.line(
                color("  · ", CLR_DIM)
                + color(str(row.tool_name), CLR_CYAN)
                + color(f" {_compact(row.arguments or {}, 120)}", CLR_DIM)
            )
        writer.line(color("[堡垒机] 直接回车 = 拒绝；否则请输入管理员账号与密码（密码不回显）", CLR_DIM))

        username, aborted = read_line("管理员账号：")
        if aborted:
            return
        if not username:
            for row in pending_rows:
                ai_service.reject_tool_call(
                    row,
                    rejected_by=caller.username,
                    reason="SSH 会话里拒绝了该敏感操作",
                    actor_id=caller.id,
                    ip=caller.ip,
                )
            writer.line(color("[堡垒机] 已拒绝该操作。", CLR_YELLOW))
            return

        password, aborted = read_line("管理员密码：", mask=True)
        if aborted:
            return
        checker = (
            User.query.filter(User.username == username.strip()).first()
            or User.query.filter(User.username == username.strip().lower()).first()
        )
        if checker is None or not checker.is_active() or not is_admin(checker) or not verify_password(password, checker.password_hash):
            for row in pending_rows:
                ai_service.reject_tool_call(
                    row,
                    rejected_by=caller.username,
                    reason="SSH 会话里管理员确认失败",
                    actor_id=caller.id,
                    ip=caller.ip,
                )
            writer.line(color("[堡垒机] 管理员账号或密码错误，已拒绝该操作。", CLR_RED))
            return

        for row in pending_rows:
            ai_service.approve_tool_call(
                row,
                approved_by=checker.username,
                actor_id=checker.id,
                ip=caller.ip,
            )
        writer.line(color(f"[堡垒机] {checker.username} 已确认，继续执行……", CLR_GREEN))
        writer.line()
        user_text = ""
        resume = True


def _compact(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else _json_dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)
