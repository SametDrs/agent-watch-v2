"""Link Gemini agy conversations to parent Claude Code sessions and subagents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
import shlex
from typing import Any
from urllib.parse import unquote

from agent_watch import config
from agent_watch.model import STANDALONE_SESSION_KEY, ShellCall, effort_from_model
from agent_watch.sources.codex import CodexConv

SESSION_PATH_RE = re.compile(
    r"(?:^|[\\/])temp[\\/]claude[\\/][^\\/]+[\\/]([0-9a-f-]{36})(?=[\\/]|$)",
    re.IGNORECASE,
)

_AGY_TOKEN_RE = re.compile(r"(?<![\w-])agy(?:\.exe)?(?![\w-])", re.IGNORECASE)
_AGY_PROMPT_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-p(?=\s|\"|'|=|$)|--(?:print|prompt)(?=\s|=|$))"
)
_USER_REQUEST_TAG_RE = re.compile(r"</?USER_REQUEST\b[^>]*>", re.IGNORECASE)
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_AGY_FLAGS = frozenset(
    {"-p", "--print", "--prompt", "--add-dir", "--conversation", "--model", "--mode"}
)
_CODEX_CMD_RE = re.compile(r"^(?:.*[/\\])?codex(?:\.exe|\.cmd)?$", re.IGNORECASE)
_NPX_CMD_RE = re.compile(r"^(?:.*[/\\])?npx(?:\.exe|\.cmd)?$", re.IGNORECASE)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CODEX_CMD_START_RE = re.compile(
    r'(?:^|(?:&&|\|\||[;|&]|\b[A-Za-z_][A-Za-z0-9_]*=\S+)\s+)'
    r'(?:npx(?:\.exe|\.cmd)?\s+(?:-\S+\s+)*)?'
    r'(?:["\']?(?:[^\s"\']*[/\\])?codex(?:\.exe|\.cmd)?["\']?)(?=\s|$)',
    re.IGNORECASE,
)
_CODEX_PACKAGE_TOKENS = frozenset({"codex", "codex.exe", "@openai/codex"})
_CODEX_NON_RUN_SUBCOMMANDS = frozenset({
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "app-server",
    "completion",
    "debug",
    "features",
    "help",
    "status",
})
_CODEX_VALUE_FLAGS = frozenset({
    "-m", "--model",
    "-c", "--config",
    "-C", "--cd",
    "-s", "--sandbox",
    "-a", "--ask-for-approval",
    "-p", "--profile",
    "-i", "--image",
    "-o", "--output-last-message",
    "--output-schema",
    "--add-dir",
    "--color",
    "--enable",
    "--disable",
    "--local-provider",
    "--oss-provider",
})
_REDIR_OPS = frozenset({"<", ">", ">>", "<<", "1>", "1>>", "2>", "2>>", "&>", "&>>"})


@dataclass
class AgyConv:
    """Summary of an agy conversation needed for session linking."""

    conv_id: str
    first_input_ts: datetime | None
    first_input_text: str
    workspace_paths: list[str] = field(default_factory=list)
    cwd: str = ""


@dataclass(frozen=True)
class Link:
    """Resolved link between an agy or Codex conversation and a Claude session/agent."""

    session_key: str
    parent_agent_key: str | None
    method: str
    score: float = 0.0
    effort: str = ""
    call_id: str | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class _ParsedCall:
    idx: int
    call: ShellCall
    is_agy: bool
    prompt: str
    add_dirs: frozenset[str]
    conversation_id: str | None
    effort: str
    ts_start: datetime
    win_start: datetime
    win_end: datetime


@dataclass(frozen=True)
class _ScoreEdge:
    score: float
    conv_id: str
    call_idx: int
    text_matched: bool
    call: ShellCall
    effort: str = ""


def normalize_path(p: str) -> str:
    """URL-decode, strip file:///, unify slashes to '/', lowercase, strip trailing '/'."""
    if not p:
        return ""
    s = unquote(p.strip()).replace("\\", "/")
    if s.lower().startswith("file:///"):
        s = s[8:]
    s = s.lower()
    if s != "/":
        s = s.rstrip("/")
    return s


def _unquote(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _has_prompt_flag_token(tok: str) -> bool:
    if tok in ("-p", "--print", "--prompt"):
        return True
    if tok.startswith(("-p=", '-p"', "-p'", "--print=", "--prompt=")):
        return True
    return False


def _has_any_agy_flag(tokens: list[str]) -> bool:
    for tok in tokens:
        flag = tok.split("=", 1)[0]
        if flag in _AGY_FLAGS or _has_prompt_flag_token(tok):
            return True
    return False


def is_agy_command(cmd: str) -> bool:
    """Return True if cmd invokes agy with a conversation-creating prompt flag."""
    if not cmd or not isinstance(cmd, str):
        return False
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return bool(_AGY_TOKEN_RE.search(cmd) and _AGY_PROMPT_FLAG_RE.search(cmd))
    for seg in _split_segments(tokens):
        has_agy = any(_AGY_TOKEN_RE.search(_unquote(tok)) for tok in seg)
        has_prompt = any(_has_prompt_flag_token(tok) for tok in seg)
        if has_agy and has_prompt:
            return True
    return False


def _select_agy_slice(tokens: list[str]) -> list[str]:
    """Return the token slice belonging to the agy invocation."""
    segments = _split_segments(tokens)
    best_seg: list[str] | None = None
    best_agy_idx = -1
    best_rank = -1

    for seg in segments:
        agy_indices = [
            i for i, tok in enumerate(seg) if _AGY_TOKEN_RE.search(_unquote(tok))
        ]
        if not agy_indices:
            continue
        has_prompt = any(_has_prompt_flag_token(tok) for tok in seg)
        has_flags = _has_any_agy_flag(seg)
        rank = (2 if has_prompt else 0) + (1 if has_flags else 0)
        if rank >= best_rank:
            best_rank = rank
            best_seg = seg
            best_agy_idx = agy_indices[-1]

    if best_seg is not None:
        return best_seg[best_agy_idx + 1 :]
    return tokens


def _parse_tokens(tokens: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "prompt": "",
        "add_dirs": [],
        "conversation_id": None,
        "model": None,
        "mode": None,
    }
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if "=" in tok and (tok.startswith("--") or tok.startswith("-p=")):
            flag, raw_val = tok.split("=", 1)
            val = _unquote(raw_val)
            if flag in ("-p", "--print", "--prompt"):
                result["prompt"] = val
            elif flag == "--add-dir":
                result["add_dirs"].append(val)
            elif flag == "--conversation":
                result["conversation_id"] = val
            elif flag == "--model":
                result["model"] = val
            elif flag == "--mode":
                result["mode"] = val
            i += 1
            continue

        if tok.startswith(('-p"', "-p'")) and len(tok) > 2:
            result["prompt"] = _unquote(tok[2:])
            i += 1
            continue

        if tok in _AGY_FLAGS:
            if i + 1 < n and not tokens[i + 1].startswith("-"):
                val = _unquote(tokens[i + 1])
                if tok in ("-p", "--print", "--prompt"):
                    result["prompt"] = val
                elif tok == "--add-dir":
                    result["add_dirs"].append(val)
                elif tok == "--conversation":
                    result["conversation_id"] = val
                elif tok == "--model":
                    result["model"] = val
                elif tok == "--mode":
                    result["mode"] = val
                i += 2
                continue
        i += 1
    return result


def _extract_flag_regex(text: str, flag_pattern: str) -> list[str]:
    pattern = re.compile(
        rf'(?:^|\s){flag_pattern}(?:\s+|=)(?:"([^"]*)"|\'([^\']*)\'|(\S+))'
    )
    values: list[str] = []
    for m in pattern.finditer(text):
        val = m.group(1) if m.group(1) is not None else (
            m.group(2) if m.group(2) is not None else m.group(3)
        )
        if val is not None:
            values.append(val)
    return values


def _parse_command_regex(cmd: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "prompt": "",
        "add_dirs": [],
        "conversation_id": None,
        "model": None,
        "mode": None,
    }
    agy_matches = list(_AGY_TOKEN_RE.finditer(cmd))
    search_text = cmd[agy_matches[-1].end() :] if agy_matches else cmd

    prompt = ""
    quoted_prompt_re = re.compile(
        r'(?:^|\s)(?:-p|--print|--prompt)(?:\s+|=)(?:"([^"]*)"|\'([^\']*)\')',
        re.DOTALL,
    )
    m_q = quoted_prompt_re.search(search_text)
    if m_q:
        prompt = m_q.group(1) if m_q.group(1) is not None else (m_q.group(2) or "")
    else:
        unquoted_prompt_re = re.compile(
            r"(?:^|\s)(?:-p|--print|--prompt)(?:\s+|=)(.+?)(?=\s+(?:--[a-zA-Z0-9_-]+|&&|\|\||;|\|)|$)",
            re.DOTALL,
        )
        m_u = unquoted_prompt_re.search(search_text)
        if m_u:
            raw = m_u.group(1).strip()
            if raw and raw[0] in ('"', "'") and (len(raw) == 1 or raw[-1] != raw[0]):
                raw = raw[1:]
            prompt = _unquote(raw)

    result["prompt"] = prompt
    result["add_dirs"] = _extract_flag_regex(search_text, r"--add-dir")
    convs = _extract_flag_regex(search_text, r"--conversation")
    if convs:
        result["conversation_id"] = convs[-1]
    models = _extract_flag_regex(search_text, r"--model")
    if models:
        result["model"] = models[-1]
    modes = _extract_flag_regex(search_text, r"--mode")
    if modes:
        result["mode"] = modes[-1]
    return result


def parse_agy_command(cmd: str) -> dict[str, Any]:
    """Extract prompt, add_dirs, conversation_id, model, and mode from an agy command."""
    empty: dict[str, Any] = {
        "prompt": "",
        "add_dirs": [],
        "conversation_id": None,
        "model": None,
        "mode": None,
    }
    if not cmd or not isinstance(cmd, str):
        return empty
    try:
        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError:
            return _parse_command_regex(cmd)
        return _parse_tokens(_select_agy_slice(tokens))
    except Exception:
        return empty


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    stripped = _USER_REQUEST_TAG_RE.sub(" ", text)
    return " ".join(stripped.split()).lower()


def _texts_match(conv_text: str, prompt_text: str) -> bool:
    """Return True if normalized input and prompt share a >=20-char substring/prefix."""
    norm_conv = _normalize_text(conv_text)
    norm_prompt = _normalize_text(prompt_text)
    conv_prefix = norm_conv[:120]
    prompt_prefix = norm_prompt[:120]
    min_len = min(len(conv_prefix), len(prompt_prefix))
    if min_len < 20:
        return False
    return (
        conv_prefix in norm_prompt
        or prompt_prefix in norm_conv
        or conv_prefix[:min_len] == prompt_prefix[:min_len]
    )


def _extract_session_uuid(conv: AgyConv) -> str | None:
    candidates = [p for p in (conv.workspace_paths or ()) if p]
    if conv.cwd:
        candidates.append(conv.cwd)
    for raw_path in candidates:
        for probe in (raw_path, normalize_path(raw_path)):
            match = SESSION_PATH_RE.search(probe)
            if match:
                return match.group(1).lower()
    return None


def _build_score_edges(
    convs: list[AgyConv],
    agy_calls: list[_ParsedCall],
) -> list[_ScoreEdge]:
    edges: list[_ScoreEdge] = []
    for conv in convs:
        if conv.first_input_ts is None:
            continue
        ts = _to_utc(conv.first_input_ts)
        in_window = [c for c in agy_calls if c.win_start <= ts <= c.win_end]
        if not in_window:
            continue
        conv_ws = {normalize_path(p) for p in (conv.workspace_paths or ()) if p}
        for c in in_window:
            text_matched = _texts_match(conv.first_input_text, c.prompt)
            dir_matched = bool(conv_ws and c.add_dirs and (conv_ws & c.add_dirs))
            delta_sec = abs((ts - c.ts_start).total_seconds())
            score = (
                (100.0 if text_matched else 0.0)
                + (20.0 if dir_matched else 0.0)
                - delta_sec
            )
            if text_matched:
                if score <= 0.0:
                    continue
            elif len(in_window) != 1:
                continue
            edges.append(
                _ScoreEdge(
                    score=score,
                    conv_id=conv.conv_id,
                    call_idx=c.idx,
                    text_matched=text_matched,
                    call=c.call,
                    effort=c.effort,
                )
            )
    edges.sort(
        key=lambda e: (-e.score, e.conv_id, _to_utc(e.call.ts_start), e.call.tool_use_id, e.call_idx)
    )
    return edges


def _assign_by_score(
    convs: list[AgyConv],
    agy_calls: list[_ParsedCall],
    used_call_indices: set[int],
) -> dict[str, tuple[ShellCall, float, str]]:
    """Greedily assign convs to calls (1:1 first, then reuse if text_matched)."""
    edges = _build_score_edges(convs, agy_calls)
    assigned: dict[str, tuple[ShellCall, float, str]] = {}

    for edge in edges:
        if edge.conv_id in assigned or edge.call_idx in used_call_indices:
            continue
        assigned[edge.conv_id] = (edge.call, edge.score, edge.effort)
        used_call_indices.add(edge.call_idx)

    for edge in edges:
        if edge.conv_id in assigned or not edge.text_matched:
            continue
        assigned[edge.conv_id] = (edge.call, edge.score, edge.effort)

    return assigned


def _precompute_calls(calls: list[ShellCall]) -> list[_ParsedCall]:
    parsed_calls: list[_ParsedCall] = []
    for idx, call in enumerate(calls):
        parsed = parse_agy_command(call.command)
        ts_start = _to_utc(call.ts_start)
        ts_end = (
            _to_utc(call.ts_end)
            if call.ts_end is not None
            else ts_start + timedelta(hours=1)
        )
        add_dirs = frozenset(
            norm for d in parsed["add_dirs"] if d and (norm := normalize_path(d))
        )
        parsed_calls.append(
            _ParsedCall(
                idx=idx,
                call=call,
                is_agy=is_agy_command(call.command),
                prompt=parsed["prompt"],
                add_dirs=add_dirs,
                conversation_id=parsed["conversation_id"],
                effort=effort_from_model(parsed["model"] or ""),
                ts_start=ts_start,
                win_start=ts_start - timedelta(seconds=2),
                win_end=ts_end + timedelta(seconds=5),
            )
        )
    return parsed_calls


def link(
    convs: list[AgyConv],
    calls: list[ShellCall],
    existing: dict[str, Link] | None = None,
) -> dict[str, Link]:
    """Link all agy conversations to Claude sessions/subagents or standalone."""
    existing_map = existing or {}
    links: dict[str, Link] = {}
    pending: list[AgyConv] = []

    for conv in convs:
        prev = existing_map.get(conv.conv_id)
        if prev is not None and prev.method != "standalone":
            links[conv.conv_id] = prev
        else:
            pending.append(conv)

    if not pending:
        return links

    parsed_calls = _precompute_calls(calls)
    agy_calls = [c for c in parsed_calls if c.is_agy]
    used_call_indices: set[int] = set()

    # Rule 1: path match via SESSION_PATH_RE.
    path_groups: dict[str, list[AgyConv]] = {}
    after_path: list[AgyConv] = []
    for conv in pending:
        session_uuid = _extract_session_uuid(conv)
        if session_uuid is not None:
            path_groups.setdefault(session_uuid, []).append(conv)
        else:
            after_path.append(conv)

    for session_uuid, group_convs in path_groups.items():
        session_agy_calls = [
            c for c in agy_calls if c.call.session_id.lower() == session_uuid
        ]
        matched = _assign_by_score(group_convs, session_agy_calls, used_call_indices)
        resolved_session_key = (
            session_agy_calls[0].call.session_id if session_agy_calls else session_uuid
        )
        for conv in group_convs:
            match_info = matched.get(conv.conv_id)
            parent_agent = match_info[0].agent_key if match_info is not None else None
            effort = match_info[2] if match_info is not None else ""
            links[conv.conv_id] = Link(
                session_key=resolved_session_key,
                parent_agent_key=parent_agent,
                method="path",
                effort=effort,
            )

    # Rule 2: conversation_id match via --conversation flag.
    after_conv_id: list[AgyConv] = []
    for conv in after_path:
        if not conv.conv_id:
            after_conv_id.append(conv)
            continue
        id_matches = [
            c for c in parsed_calls if c.conversation_id == conv.conv_id
        ]
        if id_matches:
            if conv.first_input_ts is not None:
                ts = _to_utc(conv.first_input_ts)
                chosen = min(
                    id_matches,
                    key=lambda c: (
                        c.idx in used_call_indices,
                        abs((ts - c.ts_start).total_seconds()),
                        c.idx,
                    ),
                )
            else:
                chosen = min(
                    id_matches,
                    key=lambda c: (c.idx in used_call_indices, c.ts_start, c.idx),
                )
            used_call_indices.add(chosen.idx)
            links[conv.conv_id] = Link(
                session_key=chosen.call.session_id,
                parent_agent_key=chosen.call.agent_key,
                method="conversation_id",
                effort=chosen.effort,
            )
        else:
            after_conv_id.append(conv)

    # Rule 3: score-based matching.
    scored = _assign_by_score(after_conv_id, agy_calls, used_call_indices)
    for conv in after_conv_id:
        match_info = scored.get(conv.conv_id)
        if match_info is not None:
            call, score, effort = match_info
            links[conv.conv_id] = Link(
                session_key=call.session_id,
                parent_agent_key=call.agent_key,
                method="score",
                score=score,
                effort=effort,
            )
        else:
            # Rule 4: standalone fallback.
            links[conv.conv_id] = Link(
                session_key=STANDALONE_SESSION_KEY,
                parent_agent_key=None,
                method="standalone",
            )

    return links


def _is_quoted(tok: str) -> bool:
    return len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'")


def _find_codex_slice(seg: list[str]) -> list[str] | None:
    """Return tokens after the codex executable in seg, or None if not a codex invocation."""
    i = 0
    n = len(seg)
    while i < n:
        raw = seg[i]
        tok = _unquote(raw)
        if tok == "&" and i == 0:
            i += 1
            continue
        if not _is_quoted(raw) and _ENV_ASSIGN_RE.match(raw):
            i += 1
            continue
        if tok == "env" and not _is_quoted(raw):
            i += 1
            continue
        break
    if i >= n:
        return None
    cmd_tok = _unquote(seg[i])
    if _CODEX_CMD_RE.match(cmd_tok):
        return seg[i + 1 :]
    if _NPX_CMD_RE.match(cmd_tok):
        j = i + 1
        while j < n:
            sub = _unquote(seg[j])
            if sub in ("-p", "--package") and j + 1 < n:
                j += 2
                continue
            if sub.startswith("-"):
                j += 1
                continue
            if _CODEX_CMD_RE.match(sub) or sub.lower() in _CODEX_PACKAGE_TOKENS:
                return seg[j + 1 :]
            return None
    return None


def _parse_codex_config_item(raw_item: str) -> tuple[str, str] | None:
    """Parse key=value from a codex -c/--config argument, unquoting quotes."""
    item = _unquote(raw_item.strip())
    if "=" not in item:
        return None
    key, raw_val = item.split("=", 1)
    return key.strip(), _unquote(raw_val.strip())


def _apply_codex_flag(result: dict[str, Any], flag: str, raw_val: str) -> None:
    """Apply a parsed codex flag/value pair to result."""
    val = _unquote(raw_val)
    if flag in ("-m", "--model"):
        result["model"] = val or None
    elif flag in ("-C", "--cd"):
        result["cwd"] = val or None
    elif flag in ("-c", "--config"):
        parsed_cfg = _parse_codex_config_item(raw_val)
        if parsed_cfg is not None and parsed_cfg[0] == "model_reasoning_effort" and parsed_cfg[1]:
            result["effort"] = parsed_cfg[1]


def _parse_codex_tokens(tokens: list[str]) -> tuple[dict[str, Any], bool]:
    """Parse codex CLI arguments and return (fields, is_run_invocation)."""
    result: dict[str, Any] = {
        "prompt": "",
        "model": None,
        "effort": None,
        "cwd": None,
    }
    has_exec = False
    prompt_parts: list[str] = []
    after_double_dash = False
    seen_non_run_subcmd = False
    seen_help_or_version = False

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        if tok in _REDIR_OPS:
            i += 2
            continue
        if not _is_quoted(tok) and (
            tok.startswith(("<", ">", "1>", "2>", "&>"))
            or re.match(r"^\d+>&\d+$", tok)
        ):
            i += 1
            continue

        if after_double_dash:
            if tok != "-" or prompt_parts:
                prompt_parts.append(_unquote(tok))
            i += 1
            continue

        if tok == "--":
            after_double_dash = True
            i += 1
            continue

        if "=" in tok and (
            tok.startswith("--") or tok.startswith(("-m=", "-c=", "-C="))
        ):
            flag, raw_val = tok.split("=", 1)
            _apply_codex_flag(result, flag, raw_val)
            i += 1
            continue

        if tok in _CODEX_VALUE_FLAGS:
            if i + 1 < n and not tokens[i + 1].startswith("-"):
                _apply_codex_flag(result, tok, tokens[i + 1])
                i += 2
                continue
            i += 1
            continue

        if tok in ("-h", "--help", "-V", "--version") and not prompt_parts:
            seen_help_or_version = True
            i += 1
            continue

        if tok.startswith("-"):
            i += 1
            continue

        unquoted = _unquote(tok)
        if not has_exec and not prompt_parts and not _is_quoted(tok):
            lowered = unquoted.lower()
            if lowered in ("exec", "e"):
                has_exec = True
                i += 1
                continue
            if lowered in _CODEX_NON_RUN_SUBCOMMANDS:
                seen_non_run_subcmd = True
                break

        prompt_parts.append(unquoted)
        i += 1

    result["prompt"] = " ".join(part for part in prompt_parts if part).strip()
    is_run = (not seen_non_run_subcmd) and (not seen_help_or_version) and (has_exec or bool(result["prompt"]))
    return result, is_run


def _parse_codex_command_regex(cmd: str) -> tuple[dict[str, Any], bool]:
    """Regex fallback for codex command parsing when shlex fails on unmatched quotes."""
    result: dict[str, Any] = {
        "prompt": "",
        "model": None,
        "effort": None,
        "cwd": None,
    }
    matches = list(_CODEX_CMD_START_RE.finditer(cmd))
    if not matches:
        return result, False
    search_text = cmd[matches[-1].end() :]

    models = _extract_flag_regex(search_text, r"(?:-m|--model)")
    if models:
        result["model"] = models[-1] or None
    cwds = _extract_flag_regex(search_text, r"(?:-C|--cd)")
    if cwds:
        result["cwd"] = cwds[-1] or None
    for raw_cfg in _extract_flag_regex(search_text, r"(?:-c|--config)"):
        parsed_cfg = _parse_codex_config_item(raw_cfg)
        if parsed_cfg is not None and parsed_cfg[0] == "model_reasoning_effort" and parsed_cfg[1]:
            result["effort"] = parsed_cfg[1]

    without_value_flags = re.sub(
        r'(?:^|\s)(?:-m|--model|-c|--config|-C|--cd|-s|--sandbox|-a|--ask-for-approval|'
        r'-p|--profile|-i|--image|-o|--output-last-message|--output-schema|--add-dir|'
        r'--color|--enable|--disable|--local-provider|--oss-provider)'
        r'(?:\s+|=)(?:"[^"]*"|\'[^\']*\'|\S+)',
        " ",
        search_text,
    )

    has_exec = False
    seen_non_run_subcmd = False
    seen_help_or_version = False
    skip_next = False
    for tok in without_value_flags.split():
        if skip_next:
            skip_next = False
            continue
        if tok in _REDIR_OPS:
            skip_next = True
            continue
        if tok.startswith(("<", ">", "1>", "2>", "&>")) or re.match(r"^\d+>&\d+$", tok):
            continue
        if tok == "--":
            break
        if tok in ("-h", "--help", "-V", "--version"):
            seen_help_or_version = True
            break
        if tok.startswith("-"):
            continue
        lowered = tok.lower()
        if not has_exec and lowered in ("exec", "e"):
            has_exec = True
            continue
        if not has_exec and lowered in _CODEX_NON_RUN_SUBCOMMANDS:
            seen_non_run_subcmd = True
        break

    rest = re.sub(r"^\s*(?:exec|e)(?=\s|$)", "", without_value_flags, count=1, flags=re.IGNORECASE)
    rest = re.sub(r"(?:^|\s)(?:--?[A-Za-z0-9_-]+|&&|\|\||[;|]|(?:\d*>>?|\d*<<?)\s*\S+)", " ", rest)
    raw_prompt = rest.strip()
    if raw_prompt == "-":
        raw_prompt = ""
    if raw_prompt and raw_prompt[0] in ('"', "'") and (len(raw_prompt) == 1 or raw_prompt[-1] != raw_prompt[0]):
        raw_prompt = raw_prompt[1:]
    result["prompt"] = _unquote(raw_prompt).strip()
    is_run = (not seen_non_run_subcmd) and (not seen_help_or_version) and (has_exec or bool(result["prompt"]))
    return result, is_run


def is_codex_command(cmd: str) -> bool:
    """Return True if cmd invokes codex/codex.exe/npx codex with exec/e or a positional prompt."""
    if not cmd or not isinstance(cmd, str):
        return False
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return _parse_codex_command_regex(cmd)[1]
    for seg in _split_segments(tokens):
        sl = _find_codex_slice(seg)
        if sl is not None and _parse_codex_tokens(sl)[1]:
            return True
    return False


def parse_codex_command(cmd: str) -> dict[str, Any]:
    """Extract prompt, model, effort (-c model_reasoning_effort=...), and cwd from a codex command."""
    empty: dict[str, Any] = {
        "prompt": "",
        "model": None,
        "effort": None,
        "cwd": None,
    }
    if not cmd or not isinstance(cmd, str):
        return empty
    try:
        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError:
            return _parse_codex_command_regex(cmd)[0]
        best_parsed: dict[str, Any] | None = None
        for seg in _split_segments(tokens):
            sl = _find_codex_slice(seg)
            if sl is None:
                continue
            parsed, is_run = _parse_codex_tokens(sl)
            if is_run:
                return parsed
            if best_parsed is None:
                best_parsed = parsed
        return best_parsed if best_parsed is not None else empty
    except Exception:
        return empty


@dataclass(frozen=True)
class _ParsedCodexCall:
    idx: int
    call: ShellCall
    prompt: str
    effort: str
    ts_start: datetime
    win_start: datetime
    win_end: datetime


def _precompute_codex_calls(calls: list[ShellCall]) -> list[_ParsedCodexCall]:
    parsed_calls: list[_ParsedCodexCall] = []
    for idx, call in enumerate(calls):
        if not is_codex_command(call.command):
            continue
        parsed = parse_codex_command(call.command)
        ts_start = _to_utc(call.ts_start)
        ts_end = (
            _to_utc(call.ts_end)
            if call.ts_end is not None
            else ts_start + timedelta(hours=1)
        )
        effort = parsed["effort"] or effort_from_model(parsed["model"] or "")
        parsed_calls.append(
            _ParsedCodexCall(
                idx=idx,
                call=call,
                prompt=parsed["prompt"],
                effort=effort,
                ts_start=ts_start,
                win_start=ts_start - timedelta(seconds=2),
                win_end=max(
                    ts_end + timedelta(seconds=5),
                    ts_start + timedelta(seconds=config.CODEX_LINK_WINDOW_SECONDS),
                ),
            )
        )
    return parsed_calls


def _mark_existing_codex_calls(
    existing_map: dict[str, Link],
    codex_calls: list[_ParsedCodexCall],
    conv_by_id: dict[str, CodexConv],
    used_call_indices: set[int],
) -> None:
    """Reserve calls already linked in existing_map so 1:1 invariants hold across scans."""
    for rollout_id, prev in existing_map.items():
        if prev.method == "standalone":
            continue
        if prev.call_id is not None:
            match = next(
                (c for c in codex_calls if c.idx not in used_call_indices and c.call.tool_use_id == prev.call_id),
                None,
            )
            if match is not None:
                used_call_indices.add(match.idx)
            continue
        candidates = [
            c
            for c in codex_calls
            if c.idx not in used_call_indices
            and c.call.session_id == prev.session_key
            and c.call.agent_key == prev.parent_agent_key
        ]
        if not candidates:
            continue
        conv = conv_by_id.get(rollout_id)
        if conv is not None:
            conv_ts = _to_utc(conv.created)
            chosen = min(
                candidates,
                key=lambda c: (
                    not _texts_match(conv.first_prompt, c.prompt),
                    abs((conv_ts - c.ts_start).total_seconds()),
                    c.idx,
                ),
            )
        else:
            chosen = candidates[0]
        used_call_indices.add(chosen.idx)


def link_codex(
    convs: list[CodexConv],
    calls: list[ShellCall],
    existing: dict[str, Link] | None = None,
) -> dict[str, Link]:
    """Link Codex rollouts strictly 1:1 to Claude shell calls by score, then time window."""
    existing_map = existing or {}
    links: dict[str, Link] = {}
    pending: list[CodexConv] = []

    for conv in convs:
        prev = existing_map.get(conv.rollout_id)
        if prev is not None and prev.method != "standalone":
            links[conv.rollout_id] = prev
        else:
            pending.append(conv)

    if not pending:
        return links

    codex_calls = _precompute_codex_calls(calls)
    if not codex_calls:
        return links

    used_call_indices: set[int] = set()
    conv_by_id = {conv.rollout_id: conv for conv in convs}
    _mark_existing_codex_calls(existing_map, codex_calls, conv_by_id, used_call_indices)

    # Pass 1: greedy 1:1 score matching (+100 for >=20-char normalized prompt match, - abs(dt)).
    edges: list[_ScoreEdge] = []
    for conv in pending:
        conv_ts = _to_utc(conv.created)
        for c in codex_calls:
            if c.idx in used_call_indices:
                continue
            if not (c.win_start <= conv_ts <= c.win_end):
                continue
            if not _texts_match(conv.first_prompt, c.prompt):
                continue
            delta_sec = abs((conv_ts - c.ts_start).total_seconds())
            score = 100.0 - delta_sec
            if score <= 0.0:
                continue
            edges.append(
                _ScoreEdge(
                    score=score,
                    conv_id=conv.rollout_id,
                    call_idx=c.idx,
                    text_matched=True,
                    call=c.call,
                    effort=c.effort,
                )
            )

    edges.sort(
        key=lambda e: (-e.score, e.conv_id, _to_utc(e.call.ts_start), e.call.tool_use_id, e.call_idx)
    )
    for edge in edges:
        if edge.conv_id in links or edge.call_idx in used_call_indices:
            continue
        links[edge.conv_id] = Link(
            session_key=edge.call.session_id,
            parent_agent_key=edge.call.agent_key,
            method="score",
            score=edge.score,
            effort=edge.effort,
            call_id=edge.call.tool_use_id,
        )
        used_call_indices.add(edge.call_idx)

    # Pass 2: fallback to first unclaimed rollout created within CODEX_LINK_WINDOW_SECONDS after call ts_start.
    unclaimed_convs = sorted(
        (conv for conv in pending if conv.rollout_id not in links),
        key=lambda conv: (_to_utc(conv.created), conv.rollout_id),
    )
    unclaimed_calls = sorted(
        (c for c in codex_calls if c.idx not in used_call_indices),
        key=lambda c: (c.ts_start, c.call.tool_use_id, c.idx),
    )
    window_secs = float(config.CODEX_LINK_WINDOW_SECONDS)
    for c in unclaimed_calls:
        for conv in unclaimed_convs:
            if conv.rollout_id in links:
                continue
            dt = (_to_utc(conv.created) - c.ts_start).total_seconds()
            if 0.0 <= dt <= window_secs:
                links[conv.rollout_id] = Link(
                    session_key=c.call.session_id,
                    parent_agent_key=c.call.agent_key,
                    method="window",
                    score=0.0,
                    effort=c.effort,
                    call_id=c.call.tool_use_id,
                )
                used_call_indices.add(c.idx)
                break

    return links
