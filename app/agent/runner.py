"""Agent loop: LLM (Groq) + tools, with the conversation history persisted per chat.

History is stored in OpenAI-style message format (user / system / assistant
with tool_calls / tool). When a chat gets long we start a fresh history with
a short recap.
"""

import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import llm
from app.agent.prompts import SALES_AGENT_SYSTEM
from app.agent.sales_tools import HANDLERS, TOOLS, ToolContext
from app.models import ChatMessage, Conversation
from app.services import crm
from app.util import norm_phone, now, today

log = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_HISTORY = 80
FALLBACK_REPLY = "Thank you for your message. Our sales team will reply here shortly. 🙏"

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


@dataclass
class AgentReply:
    text: str
    attachments: list[Path] = field(default_factory=list)
    silent: bool = False  # human has taken over; nothing to send
    message_id: int = 0  # id of the stored reply, so chat windows can poll for newer messages


def _run_tool(handlers: dict, ctx, name: str, raw_args: str) -> str:
    handler = handlers.get(name)
    if handler is None:
        return f"Error: unknown tool {name}"
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return f"Error: invalid JSON arguments: {e}"
    try:
        return json.dumps(handler(ctx, **args), default=str, ensure_ascii=False)
    except TypeError as e:  # wrong/missing argument names from the model
        return f"Error: invalid arguments: {e}"
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Error: tool failed: {e}"


def run_tool_loop(*, system: str, history: list, tools: list, handlers: dict, ctx, client=None,
                  effort: str | None = None) -> tuple[str, bool]:
    """Run the model until it stops calling tools. Mutates `history`. Returns (final_text, ok)."""
    for _ in range(MAX_STEPS):
        resp = llm.create(system=system, messages=history, tools=tools, effort=effort, client=client)
        choice = resp.choices[0]
        history.append(llm.assistant_entry(choice.message))
        if choice.finish_reason == "length":
            return llm.text_of(resp), False
        if not choice.message.tool_calls:
            text = llm.text_of(resp)
            return text, bool(text)
        for tc in choice.message.tool_calls:
            history.append({"role": "tool", "tool_call_id": tc.id,
                            "content": _run_tool(handlers, ctx, tc.function.name, tc.function.arguments)})
    return "", False


def _usable_history(history: list | None) -> list:
    """Drop histories saved in another provider's format (e.g. from the earlier Claude build)."""
    history = list(history or [])
    if any(isinstance(m.get("content"), list) for m in history):
        return []
    return history


def _context_note(conv: Conversation) -> str:
    parts = [f"Today is {today():%A %d %B %Y}. Channel: {conv.channel}."]
    if conv.customer:
        c = conv.customer
        parts.append(
            f"Logged-in portal user of customer account '{c.name}' ({c.city}, {c.state}; segment: "
            f"{c.segment or 'n/a'}; approved credit {c.credit_days} days). Account tools are available."
        )
    elif conv.channel == "portal":
        parts.append("Logged-in portal user who is not yet linked to a customer account (new buyer). Treat as a new "
                     "enquiry; account tools won't work. Their quotes will appear in their portal.")
    else:
        parts.append("Anonymous visitor on the website chat. Early on, ask for their name, company and a phone or "
                     "email so the team can follow up, and mention they can register on the customer portal to "
                     "track quotes. Account tools won't work; existing customers should log in to the portal.")
    if conv.lead:
        lead = conv.lead
        known = {k: getattr(lead, k) for k in ("company", "city", "state", "qty_mt", "frequency", "payment_terms")
                 if getattr(lead, k)}
        if lead.product:
            known["product"] = lead.product.name
        parts.append(f"Enquiry details already recorded: {known}")
    return " ".join(parts)


def _recap(conv: Conversation, n: int = 12) -> str:
    recent = conv.messages[-n:]
    return "\n".join(f"{'Buyer' if m.direction == 'in' else 'SHK'}: {m.body}" for m in recent)


def get_conversation(db: Session, channel: str, external_user: str) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.channel == channel,
                                                Conversation.external_user == external_user))
    if conv is None:
        conv = Conversation(channel=channel, external_user=external_user, history=[])
        db.add(conv)
        db.flush()
    return conv


def handle_inbound(db: Session, channel: str, external_user: str, text: str, *, customer=None, lead=None,
                   phone: str = "", client=None) -> AgentReply:
    """Process one buyer message and produce the reply.

    channel "web" = anonymous website chat (external_user is the browser session id);
    channel "portal" = logged-in buyer (external_user "user:<id>"), with their customer/lead passed in
    by the caller after authentication. Identity is never taken from the chat text.
    """
    with _locks[f"{channel}:{external_user}"]:
        conv = get_conversation(db, channel, external_user)
        if customer is not None and conv.customer_id != customer.id:
            conv.customer_id, conv.customer = customer.id, customer
        if lead is not None and conv.lead_id is None:
            conv.lead_id, conv.lead = lead.id, lead
        conv.last_inbound_at = now()
        db.add(ChatMessage(conversation_id=conv.id, direction="in", author="buyer", body=text))
        db.flush()
        db.refresh(conv, ["messages"])

        if conv.human_takeover:
            last = conv.messages[-1].id if conv.messages else 0
            return AgentReply(text="", silent=True, message_id=last)

        history = _usable_history(conv.history)
        note = _context_note(conv)
        if len(history) > MAX_HISTORY:
            history = []
            note += "\nRecent conversation for context:\n" + _recap(conv)
        history.append({"role": "user", "content": text})
        # Per-chat operator context, as a system message after the buyer's turn.
        history.append({"role": "system", "content": note})

        ctx = ToolContext(db=db, conversation=conv, phone=norm_phone(phone), customer=conv.customer)
        try:
            reply, ok = run_tool_loop(system=SALES_AGENT_SYSTEM, history=history, tools=TOOLS,
                                      handlers=HANDLERS, ctx=ctx, client=client)
        except llm.LLMUnavailable as e:
            log.error("LLM unavailable: %s", e)
            reply, ok = "", False

        if not ok or not reply:
            who = conv.customer.name if conv.customer else (conv.lead.company or conv.lead.name if conv.lead else "")
            crm.create_task(db, kind="handoff", title=f"AI could not answer {who or 'a website visitor'}: reply in Chats",
                            details=text, customer_id=conv.customer_id, lead_id=conv.lead_id,
                            rep_id=conv.customer.rep_id if conv.customer else (conv.lead.rep_id if conv.lead else None))
            reply = reply or FALLBACK_REPLY
        else:
            conv.history = history  # reassign so SQLAlchemy persists the JSON change
        # The chat shows plain text; drop markdown bold/headings the model sometimes adds anyway.
        reply = re.sub(r"\*\*(.+?)\*\*", r"\1", reply)
        reply = re.sub(r"(?m)^#{1,6}\s+", "", reply)

        out = ChatMessage(conversation_id=conv.id, direction="out", author="ai", body=reply)
        db.add(out)
        db.flush()
        return AgentReply(text=reply, attachments=ctx.attachments, message_id=out.id)


def staff_ask(db: Session, user: str, question: str, client=None) -> str:
    """Internal analyst chat for a dashboard user; history kept per user."""
    from app.agent import staff_tools
    from app.agent.prompts import STAFF_ASSISTANT_SYSTEM

    with _locks[f"staff:{user}"]:
        conv = get_conversation(db, "staff", user)
        db.add(ChatMessage(conversation_id=conv.id, direction="in", author=user, body=question))
        history = _usable_history(conv.history)
        if len(history) > MAX_HISTORY:
            history = []
        history.append({"role": "user", "content": question})
        history.append({"role": "system", "content": f"Today is {today():%A %d %B %Y}. Asked by: {user}."})
        try:
            answer, ok = run_tool_loop(system=STAFF_ASSISTANT_SYSTEM, history=history, tools=staff_tools.TOOLS,
                                       handlers=staff_tools.HANDLERS, ctx=staff_tools.StaffContext(db, user),
                                       client=client)
        except llm.LLMUnavailable as e:
            answer, ok = f"Assistant unavailable: {e}", False
        if ok:
            conv.history = history
        answer = answer or "I couldn't complete that. Please try rephrasing."
        db.add(ChatMessage(conversation_id=conv.id, direction="out", author="ai", body=answer))
        db.flush()
        return answer
