"""Agentic tool loop over the manual library, streamed as events."""
import asyncio
import json
import logging
import re
from datetime import date
from typing import AsyncIterator

import openai

from .. import chat_images, db
from ..config import get_settings
from ..repo import chat as chat_repo
from ..repo import maintenance as maintenance_repo
from ..repo import manuals as manuals_repo
from ..repo import notes as notes_repo
from ..search import hybrid
from . import tools as tools_mod
from .client import get_async_client
from .prompts import (FALLBACK_CONTEXT, FALLBACK_PLAN_PROMPT, FINAL_ANSWER_PROMPT, IMAGE_ONLY_TEXT, NO_TEXT_PLAN,
                      SYSTEM_PROMPT, TOOL_NOTE_INLINE, TOOL_NOTE_LARGE, TOOL_NOTE_NOTES, build_catalog, build_notes,
                      maintenance_summary)

log = logging.getLogger(__name__)

CITATION_RE = re.compile(r"\[#(\d+)\s*,?\s*pp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?\]")
NOTE_CITATION_RE = re.compile(r"\[note\s*#?\s*(\d+)\]", re.IGNORECASE)
HISTORY_TURNS = 10

# Conversations with a turn still running. A turn runs as its own task, so it finishes and is saved even if
# the browser reloads or navigates away mid-answer; the page uses this to show that an answer is on its way.
ACTIVE: set[int] = set()
_TASKS: set[asyncio.Task] = set()


class EmptyAnswerError(RuntimeError):
    """The model finished without producing any answer text."""


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


def parse_note_citations(text: str) -> list[dict]:
    ids = dict.fromkeys(int(m.group(1)) for m in NOTE_CITATION_RE.finditer(text or ""))
    return [{"note_id": i} for i in ids]


def build_system_prompt(conn) -> str:
    s = get_settings()
    rows = manuals_repo.list_ready(conn)
    catalog, truncated = build_catalog(rows, s.catalog_inline_limit)
    note_rows = notes_repo.list_all(conn)
    notes, abbreviated = build_notes(note_rows, s.notes_inline_chars, s.catalog_inline_limit)
    tool_note = (TOOL_NOTE_LARGE if truncated else TOOL_NOTE_INLINE) + (TOOL_NOTE_NOTES if abbreviated else "")
    return SYSTEM_PROMPT.format(
        tool_note=tool_note, note_count=len(note_rows), notes=notes,
        count=len(rows), catalog=catalog, today=date.today().isoformat(),
        maintenance=maintenance_summary(maintenance_repo.list_all(conn)),
    )


def history_for_llm(conn, conv_id: int) -> list[dict]:
    """Prior user/assistant turns only (tool chatter is dropped to keep context small).

    Earlier turns' images are not resent: each becomes an "[image previously attached]" marker, which
    keeps token use bounded (the answer to that turn already holds what was read from the image).
    """
    rows = [r for r in chat_repo.get_messages(conn, conv_id)
            if r["role"] == "user"
            or (r["role"] == "assistant" and not r["tool_calls"] and not r["error"] and r["content"])]
    counts = {mid: len(v) for mid, v in chat_repo.images_by_message(conn, conv_id).items()}
    return chat_repo.to_llm_history(rows, counts)[-HISTORY_TURNS * 2:]


def user_content(text: str, images: list | None):
    """A plain string, or OpenAI content parts (text first, then the images) when images are attached."""
    if not images:
        return text
    return [{"type": "text", "text": text.strip() or IMAGE_ONLY_TEXT}, *chat_images.image_parts(images)]


IMAGE_ERROR_WORDS = ("image", "vision", "multimodal", "multi-modal", "modalit")


def _is_image_unsupported(e: Exception) -> bool:
    """The provider refused the request because the model can't take image input."""
    if not isinstance(e, (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError)):
        return False
    msg = f"{getattr(e, 'message', '')} {e} {getattr(e, 'body', '')}".lower()
    return any(w in msg for w in IMAGE_ERROR_WORDS)


def image_unsupported_text(model: str) -> str:
    return (f"The current model ({model}) can't read images. Set OPENROUTER_MODEL to a vision-capable model "
            "(one that lists image input on openrouter.ai/models), or ask again without the photo.")


def _is_tool_unsupported(e: Exception) -> bool:
    msg = str(e).lower()
    return isinstance(e, (openai.NotFoundError, openai.BadRequestError)) and "tool" in msg


async def _stream_completion(messages: list[dict], *, tools: list | None, tool_choice: str | None):
    """Yields ('token', str) and finally ('result', {content, tool_calls, usage, finish_reason, error})."""
    s = get_settings()
    kwargs = dict(model=s.openrouter_model, messages=messages, stream=True,
                  stream_options={"include_usage": True}, temperature=0.2)
    if tools:
        kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    stream = await get_async_client().chat.completions.create(**kwargs)
    content, calls, usage, finish_reason, error = [], {}, None, None, None
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump()
        # OpenRouter reports failures that happen mid-stream (after HTTP 200) as an `error` on a chunk.
        if getattr(chunk, "error", None):
            error = chunk.error
        if not chunk.choices:
            continue
        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason
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
    text = "".join(content)
    if not text.strip() and not tool_calls:
        log.warning("model returned no text and no tool calls (finish_reason=%s, error=%s, usage=%s)",
                    finish_reason, error, usage)
    yield "result", {"content": text, "tool_calls": tool_calls, "usage": usage,
                     "finish_reason": finish_reason, "error": error}


def start_turn(conv_id: int, user_text: str, base_url: str | None = None,
               images: list | None = None) -> AsyncIterator[dict]:
    """Run a turn in a background task and stream its events.

    If the consumer goes away (the browser disconnects), the turn keeps running and still saves its answer.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def runner():
        ACTIVE.add(conv_id)
        try:
            async for ev in run_turn(conv_id, user_text, base_url, images=images):
                queue.put_nowait(ev)
        finally:
            ACTIVE.discard(conv_id)
            queue.put_nowait(None)

    task = asyncio.get_running_loop().create_task(runner())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    async def events():
        while (ev := await queue.get()) is not None:
            yield ev

    return events()


def _save_error(conn, conv_id: int, text: str) -> None:
    try:
        conn.rollback()
        chat_repo.add_message(conn, conv_id, "assistant", text, error=True, model=get_settings().openrouter_model)
        conn.commit()
    except Exception:
        log.exception("could not save the error message")


async def run_turn(conv_id: int, user_text: str, base_url: str | None = None,
                   images: list | None = None) -> AsyncIterator[dict]:
    """Persist the user message, run the agent, persist the answer. Yields UI events.

    base_url is the request's own URL, used for share links when APP_URL isn't set.
    images are already-validated chat_images.ProcessedImage objects attached to this question.
    """
    s = get_settings()
    images = images or []
    conn = db.connect()
    try:
        conv = chat_repo.get_conversation(conn, conv_id)
        prior = history_for_llm(conn, conv_id)
        user_msg_id = chat_repo.add_message(conn, conv_id, "user", user_text)
        for img in images:
            chat_images.save(conn, conv_id, user_msg_id, img)
        if not conv["title"]:
            chat_repo.set_title(conn, conv_id, user_text.strip()[:70] or "Photo question")
        chat_repo.touch(conn, conv_id)
        conn.commit()

        if not s.openrouter_api_key:
            yield {"type": "error", "text": "OPENROUTER_API_KEY is not configured on the server."}
            return

        messages = [{"role": "system", "content": build_system_prompt(conn)}, *prior,
                    {"role": "user", "content": user_content(user_text, images)}]

        final_text = None
        if s.llm_tools:
            try:
                async for ev in _tool_loop(conn, conv_id, messages, base_url):
                    if ev["type"] == "final":
                        final_text = ev["text"]
                    else:
                        yield ev
            except Exception as e:
                if (images and _is_image_unsupported(e)) or not _is_tool_unsupported(e):
                    raise
                log.warning("model rejected tools, using fallback pipeline: %s", e)
                yield {"type": "reset"}
        if final_text is None:
            async for ev in _fallback(conn, messages):
                if ev["type"] == "final":
                    final_text = ev["text"]
                else:
                    yield ev
        if not (final_text or "").strip():
            raise EmptyAnswerError()

        citations = parse_citations(final_text)
        known = {m["id"]: m for m in manuals_repo.list_all(conn)}
        for c in citations:
            m = known.get(c["manual_id"])
            c["title"] = (m["title"] or m["filename"]) if m else None
        for c in parse_note_citations(final_text):
            n = notes_repo.get(conn, c["note_id"])
            c["title"] = n["title"] if n else None
            citations.append(c)
        msg_id = chat_repo.add_message(conn, conv_id, "assistant", final_text, citations=citations,
                                       model=s.openrouter_model)
        conn.commit()
        yield {"type": "done", "message_id": msg_id, "citations": citations}
    except EmptyAnswerError:
        text = "The AI model finished without writing an answer. Try asking again, or rephrase the question."
        _save_error(conn, conv_id, text)
        yield {"type": "error", "text": text}
    except openai.APIError as e:
        if images and _is_image_unsupported(e):
            log.warning("model %s rejected image input: %s", s.openrouter_model, e)
            yield {"type": "error", "text": image_unsupported_text(s.openrouter_model)}
            return
        log.exception("LLM error")
        text = f"The AI service returned an error: {getattr(e, 'message', str(e))}"
        _save_error(conn, conv_id, text)
        yield {"type": "error", "text": text}
    except Exception as e:
        log.exception("chat turn failed")
        text = f"Something went wrong: {type(e).__name__}: {e}"
        _save_error(conn, conv_id, text)
        yield {"type": "error", "text": text}
    finally:
        conn.close()


async def _tool_loop(conn, conv_id: int, messages: list[dict], base_url: str | None = None) -> AsyncIterator[dict]:
    s = get_settings()
    for _ in range(s.max_tool_rounds):
        result = None
        async for kind, val in _stream_completion(messages, tools=tools_mod.TOOLS, tool_choice="auto"):
            if kind == "token":
                yield {"type": "token", "text": val}
            else:
                result = val
        if not result["tool_calls"]:
            if result["content"].strip():
                yield {"type": "final", "text": result["content"]}
                return
            break  # the model returned nothing at all: ask once more for a plain answer
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
            output, _ = tools_mod.dispatch(conn, name, call["function"]["arguments"], conv_id=conv_id,
                                           base_url=base_url)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            chat_repo.add_message(conn, conv_id, "tool", output, tool_call_id=call["id"])
        conn.commit()

    # Out of tool rounds (or the model went silent). Asking with tool_choice="none" made DeepSeek return an
    # empty message, so send no tools at all and tell the model plainly to answer with what it has found.
    final_msgs = [*messages, {"role": "system", "content": FINAL_ANSWER_PROMPT}]
    result = None
    async for kind, val in _stream_completion(final_msgs, tools=None, tool_choice=None):
        if kind == "token":
            yield {"type": "token", "text": val}
        else:
            result = val
    yield {"type": "final", "text": result["content"]}


async def _fallback(conn, messages: list[dict]) -> AsyncIterator[dict]:
    """Non-agentic pipeline: plan queries -> search -> answer with context."""
    s = get_settings()
    rows = manuals_repo.list_ready(conn)
    catalog, _ = build_catalog(rows, s.catalog_inline_limit)
    # The planner is text only: images on the current turn go to the final answer call below.
    last = messages[-1]["content"]
    question = "" if isinstance(last, list) and last[0].get("text") == IMAGE_ONLY_TEXT else chat_repo.content_text(last)
    lines = [f"{m['role']}: {chat_repo.content_text(m['content'])}" for m in messages[1:-1]
             if m["role"] in ("user", "assistant")]
    convo = "\n".join([*lines, f"user: {question or NO_TEXT_PLAN}"])
    yield {"type": "status", "text": "Choosing which manuals to search"}
    plan = {"queries": [question] if question.strip() else [], "manual_ids": []}
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
        for h in hybrid.search(conn, q, plan.get("manual_ids") or None, 6, s.preferred_lang_list):
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
