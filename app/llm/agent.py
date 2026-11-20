"""Agentic tool loop over the manual library, streamed as events."""
import json
import logging
import re
from typing import AsyncIterator

import openai

from .. import db
from ..config import get_settings
from ..repo import chat as chat_repo
from ..repo import manuals as manuals_repo
from ..search import fts
from . import tools as tools_mod
from .client import get_async_client
from .prompts import (FALLBACK_CONTEXT, FALLBACK_PLAN_PROMPT, SYSTEM_PROMPT, TOOL_NOTE_INLINE,
                      TOOL_NOTE_LARGE, build_catalog)

log = logging.getLogger(__name__)

CITATION_RE = re.compile(r"\[#(\d+)\s*,?\s*pp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?\]")
HISTORY_TURNS = 10


def parse_citations(text: str) -> list[dict]:
    seen, out = set(), []
    for m in CITATION_RE.finditer(text or ""):
        mid, a = int(m.group(1)), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        key = (mid, a, b)
        if key not in seen:
            seen.add(key)
            out.append({"manual_id": mid, "page": a, "page_end": b})
    return out


def build_system_prompt(conn) -> str:
    s = get_settings()
    rows = manuals_repo.list_ready(conn)
    catalog, truncated = build_catalog(rows, s.catalog_inline_limit)
    return SYSTEM_PROMPT.format(
        tool_note=TOOL_NOTE_LARGE if truncated else TOOL_NOTE_INLINE,
        count=len(rows), catalog=catalog,
    )


def history_for_llm(conn, conv_id: int) -> list[dict]:
    """Prior user/assistant turns only (tool chatter is dropped to keep context small)."""
    rows = chat_repo.get_messages(conn, conv_id)
    msgs = [{"role": r["role"], "content": r["content"] or ""}
            for r in rows if r["role"] == "user" or (r["role"] == "assistant" and not r["tool_calls"] and r["content"])]
    return msgs[-HISTORY_TURNS * 2:]


def _is_tool_unsupported(e: Exception) -> bool:
    msg = str(e).lower()
    return isinstance(e, (openai.NotFoundError, openai.BadRequestError)) and "tool" in msg


async def _stream_completion(messages: list[dict], *, tools: list | None, tool_choice: str | None):
    """Yields ('token', str) and finally ('result', {content, tool_calls, usage})."""
    s = get_settings()
    kwargs = dict(model=s.openrouter_model, messages=messages, stream=True,
                  stream_options={"include_usage": True}, temperature=0.2)
    if tools:
        kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    stream = await get_async_client().chat.completions.create(**kwargs)
    content, calls, usage = [], {}, None
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump()
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content.append(delta.content)
            yield "token", delta.content
        for tc in delta.tool_calls or []:
            slot = calls.setdefault(tc.index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["function"]["name"] += tc.function.name
                if tc.function.arguments:
                    slot["function"]["arguments"] += tc.function.arguments
    tool_calls = []
    for i, c in sorted(calls.items()):
        if not c["id"]:
            c["id"] = f"call_{i}"
        tool_calls.append(c)
    yield "result", {"content": "".join(content), "tool_calls": tool_calls, "usage": usage}


async def run_turn(conv_id: int, user_text: str) -> AsyncIterator[dict]:
    """Persist the user message, run the agent, persist the answer. Yields UI events."""
    s = get_settings()
    conn = db.connect()
    try:
        conv = chat_repo.get_conversation(conn, conv_id)
        prior = history_for_llm(conn, conv_id)
        chat_repo.add_message(conn, conv_id, "user", user_text)
        if not conv["title"]:
            chat_repo.set_title(conn, conv_id, user_text.strip()[:70])
        chat_repo.touch(conn, conv_id)
        conn.commit()

        if not s.openrouter_api_key:
            yield {"type": "error", "text": "OPENROUTER_API_KEY is not configured on the server."}
            return

        messages = [{"role": "system", "content": build_system_prompt(conn)}, *prior,
                    {"role": "user", "content": user_text}]

        final_text = None
        if s.llm_tools:
            try:
                async for ev in _tool_loop(conn, conv_id, messages):
                    if ev["type"] == "final":
                        final_text = ev["text"]
                    else:
                        yield ev
            except Exception as e:
                if not _is_tool_unsupported(e):
                    raise
                log.warning("model rejected tools, using fallback pipeline: %s", e)
                yield {"type": "reset"}
        if final_text is None:
            async for ev in _fallback(conn, messages):
                if ev["type"] == "final":
                    final_text = ev["text"]
                else:
                    yield ev

        citations = parse_citations(final_text)
        known = {m["id"]: m for m in manuals_repo.list_all(conn)}
        for c in citations:
            m = known.get(c["manual_id"])
            c["title"] = (m["title"] or m["filename"]) if m else None
        msg_id = chat_repo.add_message(conn, conv_id, "assistant", final_text, citations=citations,
                                       model=s.openrouter_model)
        conn.commit()
        yield {"type": "done", "message_id": msg_id, "citations": citations}
    except openai.APIError as e:
        log.exception("LLM error")
        yield {"type": "error", "text": f"The AI service returned an error: {getattr(e, 'message', str(e))}"}
    except Exception as e:
        log.exception("chat turn failed")
        yield {"type": "error", "text": f"Something went wrong: {type(e).__name__}: {e}"}
    finally:
        conn.close()


async def _tool_loop(conn, conv_id: int, messages: list[dict]) -> AsyncIterator[dict]:
    s = get_settings()
    for round_no in range(s.max_tool_rounds + 1):
        force_answer = round_no == s.max_tool_rounds
        result = None
        async for kind, val in _stream_completion(messages, tools=tools_mod.TOOLS,
                                                  tool_choice="none" if force_answer else "auto"):
            if kind == "token":
                yield {"type": "token", "text": val}
            else:
                result = val
        if not result["tool_calls"] or force_answer:
            yield {"type": "final", "text": result["content"]}
            return
        # The model decided to use tools: discard any preamble text it streamed.
        if result["content"]:
            yield {"type": "reset"}
        messages.append({"role": "assistant", "content": result["content"] or "", "tool_calls": result["tool_calls"]})
        chat_repo.add_message(conn, conv_id, "assistant", result["content"], tool_calls=result["tool_calls"],
                              model=s.openrouter_model, usage=result["usage"])
        for call in result["tool_calls"]:
            name = call["function"]["name"]
            try:
                status = tools_mod.status_text(name, json.loads(call["function"]["arguments"] or "{}"), conn)
            except Exception:
                status = name
            yield {"type": "status", "text": status}
            output, _ = tools_mod.dispatch(conn, name, call["function"]["arguments"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            chat_repo.add_message(conn, conv_id, "tool", output, tool_call_id=call["id"])
        conn.commit()


async def _fallback(conn, messages: list[dict]) -> AsyncIterator[dict]:
    """Non-agentic pipeline: plan queries -> search -> answer with context."""
    s = get_settings()
    rows = manuals_repo.list_ready(conn)
    catalog, _ = build_catalog(rows, s.catalog_inline_limit)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[1:] if m["role"] in ("user", "assistant"))
    yield {"type": "status", "text": "Choosing which manuals to search"}
    plan = {"queries": [messages[-1]["content"]], "manual_ids": []}
    try:
        resp = await get_async_client().chat.completions.create(
            model=s.openrouter_model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": FALLBACK_PLAN_PROMPT.format(catalog=catalog, question=convo[-4000:])}],
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        if parsed.get("queries"):
            plan = parsed
    except Exception as e:
        log.warning("fallback planning failed: %s", e)
    hits, seen = [], set()
    for q in plan["queries"][:4]:
        yield {"type": "status", "text": f"Searching for “{q}”"}
        for h in fts.search(conn, q, plan.get("manual_ids") or None, 6, s.preferred_lang_list):
            if h["chunk_id"] not in seen:
                seen.add(h["chunk_id"])
                hits.append(h)
    hits.sort(key=lambda h: h["score"])
    context = "\n\n---\n\n".join(
        f"[#{h['manual_id']} p.{h['page_start']}{'-' + str(h['page_end']) if h['page_end'] != h['page_start'] else ''}] "
        f"{h['manual_title']}\n{h['text']}" for h in hits[:10]
    ) or "(no matching passages found)"
    answer_msgs = [*messages[:-1], {"role": "system", "content": FALLBACK_CONTEXT.format(context=context)}, messages[-1]]
    result = None
    async for kind, val in _stream_completion(answer_msgs, tools=None, tool_choice=None):
        if kind == "token":
            yield {"type": "token", "text": val}
        else:
            result = val
    yield {"type": "final", "text": result["content"]}
