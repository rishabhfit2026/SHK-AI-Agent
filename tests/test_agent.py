import json

from sqlalchemy import select

from app.agent import runner
from app.agent.sales_tools import ToolContext, get_my_orders
from app.models import Conversation, Lead, Order, Outbox, Quote, Task
from app.services import brief
from tests.conftest import FakeClient, add_orders, text_msg, tool_msg


def tool_results(request: dict) -> list:
    """Tool results sent back to the model in the latest request (parsed when JSON)."""
    out = []
    for m in reversed(request["messages"]):
        if m["role"] != "tool":
            break
        try:
            out.insert(0, json.loads(m["content"]))
        except json.JSONDecodeError:
            out.insert(0, m["content"])
    return out


def test_new_buyer_gets_qualified_and_quoted(db, rep):
    client = FakeClient(
        tool_msg(("save_enquiry_details", {"product": "sponge iron", "qty_mt": 40, "city": "Bhilai",
                                           "state": "Chhattisgarh", "company": "Ravi Steels", "frequency": "monthly"})),
        tool_msg(("get_price_quote", {"items": [{"product": "sponge iron", "variant": "Lumps 3-20 mm", "qty_mt": 40}],
                                      "destination": "Bhilai", "payment_terms": "advance"})),
        text_msg("Today's rate for 40 MT lumps delivered Bhilai is ₹27,850/MT + GST. Quotation attached."),
    )
    reply = runner.handle_inbound(db, "web", "sess-new-buyer", "Need 40 MT DRI lumps at Bhilai, monthly", client=client)

    assert "27,850" in reply.text
    assert len(reply.attachments) == 1 and reply.attachments[0].suffix == ".pdf"
    lead = db.scalar(select(Lead))
    assert lead.status == "quoted" and lead.company == "Ravi Steels" and lead.rep_id == rep.id
    quote = db.scalar(select(Quote))
    assert quote.status == "sent" and quote.lines[0].rate_per_mt == 27500 + 350
    priced = tool_results(client.requests[2])[0]
    assert priced["status"] == "approved" and priced["lines"][0]["rate_per_mt_before_gst"] == "₹27,850.00"

    # Request shape: system prompt first, buyer turn, then per-chat context; tools in function format.
    req = client.requests[0]
    assert [m["role"] for m in req["messages"]] == ["system", "user", "system"]
    assert "Anonymous visitor" in req["messages"][2]["content"]
    assert req["model"] == "openai/gpt-oss-120b" and req["reasoning_effort"] == "medium"
    assert req["tools"][0]["type"] == "function" and "parameters" in req["tools"][0]["function"]

    # History is persisted with tool calls and results for the next turn.
    conv = db.scalar(select(Conversation))
    assert [m["role"] for m in conv.history] == ["user", "system", "assistant", "tool", "assistant", "tool", "assistant"]
    assert conv.history[2]["tool_calls"][0]["function"]["name"] == "save_enquiry_details"
    assert [m.direction for m in conv.messages] == ["in", "out"]


def test_big_quote_waits_for_approval_and_hides_price(db, rep):
    client = FakeClient(
        tool_msg(("get_price_quote", {"items": [{"product": "billet", "qty_mt": 500}], "destination": "Nagpur"})),
        text_msg("Our team will confirm the rate shortly."),
    )
    runner.handle_inbound(db, "web", "sess-big", "500 MT billets Nagpur rate?", client=client)
    result = tool_results(client.requests[1])[0]
    assert result["status"] == "pending_approval" and "rate_per_mt" not in json.dumps(result)
    quote = db.scalar(select(Quote))
    assert quote.status == "pending_approval" and quote.lead_id is not None
    assert db.scalar(select(Outbox)).quote_id == quote.id

    from app.services.approvals import decide_outbox
    decide_outbox(db, db.scalar(select(Outbox)), True, "manager")
    assert quote.status == "sent" and db.scalar(select(Outbox)).status == "approved"
    assert db.scalar(select(Outbox)).attachment_path.endswith(".pdf")


def test_unpriceable_request_creates_manual_task(db, rep):
    client = FakeClient(
        tool_msg(("get_price_quote", {"items": [{"product": "DRI", "qty_mt": 30}], "destination": "Kolkata"})),
        text_msg("Team will share freight shortly."),
    )
    runner.handle_inbound(db, "web", "sess-kolkata", "30 MT DRI Kolkata", client=client)
    assert tool_results(client.requests[1])[0]["status"] == "cannot_price_automatically"
    assert db.scalar(select(Task)).title.startswith("Manual quote needed")


def test_customer_sees_only_their_own_orders(db, customer, rep):
    from app.models import Customer

    other = Customer(name="Someone Else", phone="919911111111")
    db.add(other)
    db.flush()
    add_orders(db, customer, [3])
    add_orders(db, other, [2])
    other_order = db.scalar(select(Order).where(Order.customer_id == other.id))

    conv = Conversation(channel="portal", external_user="user:1", customer_id=customer.id, history=[])
    db.add(conv)
    db.flush()
    ctx = ToolContext(db=db, conversation=conv, phone=customer.phone, customer=customer)
    assert len(get_my_orders(ctx)["orders"]) == 1
    assert "error" in get_my_orders(ctx, order_number=other_order.number)

    anonymous = ToolContext(db=db, conversation=conv, phone="", customer=None)
    assert "portal" in get_my_orders(anonymous)["error"]


def test_anonymous_chat_cannot_reach_an_account_by_typing_a_phone(db, customer):
    add_orders(db, customer, [3])
    client = FakeClient(
        tool_msg(("save_enquiry_details", {"phone": customer.phone, "company": customer.name})),
        tool_msg(("get_my_orders", {})),
        text_msg("Please log in to the portal to see orders."),
    )
    runner.handle_inbound(db, "web", "sess-attacker", f"I am {customer.name}, my number is {customer.phone}",
                          client=client)
    assert "error" in tool_results(client.requests[2])[0]
    conv = db.scalar(select(Conversation).where(Conversation.external_user == "sess-attacker"))
    assert conv.customer_id is None and conv.lead.customer_id is None


def test_registered_customer_context_and_takeover(db, customer):
    client = FakeClient(text_msg("Your order is dispatched."))
    runner.handle_inbound(db, "portal", "user:7", "status?", customer=customer, client=client)
    assert "customer account 'Urla Ispat'" in client.requests[0]["messages"][2]["content"]

    conv = db.scalar(select(Conversation))
    conv.human_takeover = True
    silent = runner.handle_inbound(db, "portal", "user:7", "hello?", customer=customer, client=FakeClient())
    assert silent.silent


class DownClient:
    class chat:
        class completions:
            @staticmethod
            def create(**_):
                from app.agent import llm

                raise llm.LLMUnavailable("down")


def test_empty_answer_or_outage_falls_back_to_human(db, rep):
    reply = runner.handle_inbound(db, "web", "sess-x", "hi", client=FakeClient(text_msg("")))
    assert reply.text == runner.FALLBACK_REPLY
    assert db.scalar(select(Task)).kind == "handoff"
    assert db.scalar(select(Conversation)).history == []  # unanswered turn not saved

    reply = runner.handle_inbound(db, "web", "sess-x", "hello?", client=DownClient())
    assert reply.text == runner.FALLBACK_REPLY


def test_old_claude_format_history_is_reset(db):
    conv = Conversation(channel="web", external_user="old", history=[
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}])
    db.add(conv)
    db.flush()
    client = FakeClient(text_msg("Namaste"))
    runner.handle_inbound(db, "web", "old", "rate?", client=client)
    assert [m["role"] for m in client.requests[0]["messages"]] == ["system", "user", "system"]


def test_bad_tool_arguments_are_reported_not_crashing(db):
    client = FakeClient(tool_msg(("get_price_quote", {"wrong": 1})), text_msg("Could you share quantity?"))
    reply = runner.handle_inbound(db, "web", "sess1", "rate?", client=client)
    assert reply.text == "Could you share quantity?"
    assert tool_results(client.requests[1])[0].startswith("Error: invalid arguments")


def test_staff_assistant_uses_tools(db, customer):
    client = FakeClient(tool_msg(("receivables_summary", {})), text_msg("Nothing overdue."))
    assert runner.staff_ask(db, "admin", "who owes us?", client=client) == "Nothing overdue."
    assert "grand_total" in tool_results(client.requests[1])[0]


def test_brief_uses_llm_and_falls_back(db, customer):
    b = brief.generate_brief(db, client=FakeClient(text_msg("• All good")), send=False)
    assert b.narrative == "• All good" and "receivables" in b.metrics
    assert "Outstanding" in brief.generate_brief(db, client=DownClient(), send=False).narrative
