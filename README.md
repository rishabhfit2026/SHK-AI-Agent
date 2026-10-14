# SHK Sales Desk

Website-based AI sales assistant, customer portal and CRM for **Shri Hare-Krishna Sponge Iron Ltd**
(Siltara, Raipur). Everything happens on the website:

| Who | Where | What they do |
|---|---|---|
| Any visitor | `/` (and the widget on shkraipur.com) | Chat with the AI: product questions, requirement capture, today's delivered rate, PDF quotation |
| Customers | `/portal` (login) | Same AI chat, plus their orders and dispatches (truck, LR, ETA), test certificate downloads, account statement, quotes (download / accept), and a message inbox |
| SHK staff | `/staff` (login) | Leads, quote approvals, customers and reorder alerts, orders and dispatch entry, receivables, new-product pipeline, outbox approvals, chats (reply or take over), daily rates, AI analyst, Tally import |

The AI runs on Groq (`openai/gpt-oss-120b` by default).

## Quick start

```bash
uv sync
cp .env.example .env               # add GROQ_API_KEY; change DASHBOARD_PASSWORD and SECRET_KEY
uv run python -m app.seed --demo   # catalogue + sample data (omit --demo for a clean install)
uv run uvicorn app.main:app --reload
```

- Website: http://localhost:8000
- Customer portal (demo): http://localhost:8000/portal/login, `buyer@urla-demo.in` / `demo-buyer-123`
- Staff: http://localhost:8000/staff (user/password from `.env`). **Try the chat** lets staff chat as an anonymous visitor or as any customer.

Tests: `uv run pytest` (71 tests; Groq and Nemotron are replaced by fakes).

## How buyers get access
- **Anonymous visitors** can chat and get quotes right away. The AI asks for name, company and phone/email so sales can follow up. Quotes are downloadable only from that browser's chat session.
- **New buyers** can register at `/portal/register`. They see their own chats and quotes. A self-registered account is **never** linked to an existing customer automatically (typing someone's phone number must not reveal their account). Staff link it from the lead page after checking, or it's linked when the lead is marked won or a quote is converted to an order.
- **Existing customers**: staff open the customer's page → **Create portal login** → the customer gets an email with a one-time link to set their password. The link is also shown on screen if email isn't set up.

## The six modules

| # | Module | How it works |
|---|---|---|
| 1 | Enquiry agent | Website chat and portal chat → a scored **lead**, routed to a rep by state and product. |
| 2 | Quotes | Price = today's base rate + grade premium + payment-term charge + freight. A PDF link appears in the chat. Over ₹50 L, any discount, or credit beyond the customer's approved terms → staff **approval** first, and the buyer sees "being confirmed" with no number. Missing rate or unknown city → manual quote task. |
| 3 | Reorder alerts | Each customer's usual gap between orders. Overdue by 1.5× or volume down 40% → call task for the rep + a drafted check-in message. |
| 4 | Orders, dispatch, payments | Customers see orders, dispatches and MTCs in the portal and can ask the chat. Staff dispatch entry can notify the customer. Payment reminders are drafted 3 days before due, on the due date, and +7/+15/+30 days; at +30 the rep also gets a call task. |
| 5 | New-product pipeline | Grinding media balls, tooth points, alloy castings: stages with automatic next actions; the AI drafts outreach emails. |
| 6 | Daily brief and AI analyst | 08:00 brief on the staff home page (emailed to `MANAGEMENT_EMAILS`); **Ask AI** answers questions over live CRM data. |

**Messages to buyers:** everything the system drafts (reminders, reorder check-ins, quotes that needed approval, outreach) waits in **/staff/outbox** for approval. Approved messages go to the customer's **portal inbox** and, if SMTP is set, by **email**. Dispatch notices and portal invites that staff trigger go out immediately. Staff replies typed in **Chats** appear in the buyer's chat window (it checks for new messages every 15 seconds).

## Payment proofs and invoices (Nemotron OCR)
- **Customers** upload a bank slip, NEFT/RTGS/UPI screenshot or cheque photo on **Portal → Account**.
- **Staff** upload payment proofs or SHK invoices (PDF or photo) on **Staff → Documents**.
- **NVIDIA Nemotron OCR** reads the document, then the Groq LLM picks out the fields: amount, UTR, date, payer and beneficiary for payments; number, buyer, GSTIN, MT, rate, GST and total for invoices.
- **Automatic checks** flag a duplicate UTR or invoice number, a beneficiary that isn't SHK, a future or old date, totals that don't add up, an unknown customer, and low OCR confidence. For payments it also suggests which open invoice the money belongs to.
- **A staff member compares the fields with the document** (shown side by side), corrects them if needed, and clicks Confirm. Only then is the payment recorded or the invoice created. The customer gets a "payment received" message, or a "couldn't verify" message if staff reject it.
- **Setup:** set `NEMOTRON_OCR_URL` in `.env`.
  - Self-hosted NIM on a GPU server: `http://<server>:8000/v1/infer` (nemotron-ocr-v1) or `.../v1/ocr` (nemotron-ocr-v2).
  - NVIDIA-hosted endpoint: also set `NEMOTRON_OCR_API_KEY`.
  - Without OCR configured, uploads still work and staff type the fields by hand.

## Daily routine for SHK staff
1. **Rates** (morning): enter today's ex-works base rate per product. The AI won't quote a product until its rate is in.
2. **Today**: read the brief and work through open tasks.
3. **Outbox**: approve, edit or reject drafted messages.
4. **Quotes**: approve quotes over the limit; convert accepted quotes into orders.
5. **Orders**: record dispatches (truck, LR, MTC PDF), ticking "notify the customer".
6. **Chats**: answer handovers; take over a chat to pause the AI.
7. **Documents**: verify payment proofs uploaded by customers.
8. **Import**: upload Tally sales and receipt CSVs regularly (samples in `samples/`).

## Go-live checklist
- [ ] Replace **placeholder numbers** in `app/seed.py` (or via Rates): size/grade premiums, payment-term charges, freight table, minimum orders.
- [ ] Load real customers and history from Tally; add customer emails; create portal logins for active buyers.
- [ ] Add sales reps (there's no rep editor in the UI yet; add rows to `sales_reps`, including their email).
- [ ] `.env`: `GROQ_API_KEY`, a strong `DASHBOARD_PASSWORD`, a random `SECRET_KEY`, `PUBLIC_BASE_URL=https://<your domain>`, SMTP settings (any provider: Gmail Workspace, Zoho, SES…), `MANAGEMENT_EMAILS`.
- [ ] Deploy behind **HTTPS** (e.g. `portal.shkraipur.com`): `uvicorn app.main:app --host 0.0.0.0 --port 8000` behind nginx or Caddy. Run a **single worker**, since the scheduler runs in-process. Back up `data/` (database, quote PDFs, MTCs). Use Postgres via `DATABASE_URL` if many people use it at once.
- [ ] On shkraipur.com add the widget: `<script src="https://<your domain>/static/widget.js" defer></script>`, or link a "Get today's rate" button to the home page.
- [ ] All staff share one dashboard login for now; add per-user staff accounts before wider rollout.
- [ ] Have the company secretary review the investor-question rule in `app/agent/prompts.py`.

## Layout
```
app/
  main.py          app setup: sessions, CORS, routers, background scheduler
  web/public.py    home page, anonymous chat API, customer portal
  web/staff.py     staff dashboard (/staff)
  agent/           Groq wrapper, prompts, agent loop, customer + staff tools
  services/        pricing, quote PDF, CRM, reorder, receivables, orders, pipeline, brief,
                   portal accounts, notifications/email delivery, approvals, Tally import
  channels/        email (SMTP), Nemotron OCR client
  templates/       portal/ (buyer-facing) and staff/
  static/          styles, chat.js (site + portal), widget.js (embed on shkraipur.com)
tests/
```
