"""What the requester receives if staff send the suggested reply exactly as drafted.

Only the reply text goes to the requester (as a "Reply to customer" comment);
the triage section of the internal note never leaves Jira. The email layout is
copied from a real Jira Service Management notification:
reply marker, "<agent> commented:", the comment, then the request links and the
"Powered by Jira Service Management" footer. JSM does not add a greeting or a
sign-off, so the drafter adds both (drafter.compose_reply).
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from reply_drafter.drafter import SIGN_OFF, DraftResult

PORTAL = "https://jira.example.edu/servicedesk/customer/portal/1/"
_GREETING_START = ("hi ", "hello", "hey", "dear", "good morning", "good afternoon", "good evening")


def write_requester_page(
    tickets: list[tuple[DraftResult, ...]],
    requesters: dict[str, str],
    *,
    path: Path,
    operator: str,
    review_page: str,
) -> Path:
    cards = "\n".join(_ticket(drafts, requesters.get(drafts[0].target_key, "Requester"), operator)
                      for drafts in tickets)
    path.write_text(_PAGE.format(cards=cards, review_page=html.escape(review_page),
                                 stamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
                                 n=len(tickets)), encoding="utf-8")
    return path


def _ticket(drafts: tuple[DraftResult, ...], requester: str, operator: str) -> str:
    first = drafts[0]
    emails = "\n".join(_email(r, requester, operator) for r in drafts if r.reply)
    return f"""
<section class="ticket" id="{html.escape(first.target_key)}">
  <h2><span class="key">{html.escape(first.target_key)}</span> {html.escape(first.summary)}</h2>
  <div class="emails">{emails}</div>
</section>"""


def _email(r: DraftResult, requester: str, operator: str) -> str:
    model = r.llm.model if r.llm else ""
    paras = "".join(f"<p>{_lines(p)}</p>" for p in r.reply.split("\n\n") if p.strip())
    issues = []
    if not r.reply.strip().lower().startswith(_GREETING_START):
        issues.append("No greeting: the requester's name is never used.")
    if not r.reply.rstrip().endswith(SIGN_OFF):
        issues.append("No sign-off: the reply ends without a name.")
    if r.needs_human:
        issues.append("Marked \"needs a human\": staff should act or decide before sending this.")
    notes = "".join(f"<li>{html.escape(i)}</li>" for i in issues)
    notes_html = f'<ul class="notes">{notes}</ul>' if issues else '<p class="ok">Ready to send as written.</p>'
    return f"""
  <article class="mail">
    <div class="model">Draft by {html.escape(model)}</div>
    <div class="mail-head">
      <div class="subject">{html.escape(r.target_key)} {html.escape(r.summary)}</div>
      <div class="row"><span class="lbl">From</span> {html.escape(operator)} &lt;Help service desk&gt;</div>
      <div class="row"><span class="lbl">To</span> {html.escape(requester)}</div>
    </div>
    <div class="mail-body">
      <div style="color:#999999">&mdash;-&mdash;-&mdash;-&mdash;</div>
      <div style="color:#999999;margin-bottom:30px">Reply above this line.</div>
      <div style="margin-bottom:30px">
        <p>{html.escape(operator)} commented:</p>
        {paras}
      </div>
      <p><a href="{PORTAL}{html.escape(r.target_key)}" style="color:#3572b0;text-decoration:none">View request</a> ·
         <span style="color:#3572b0">Turn off this request's notifications</span></p>
      <p style="color:#707070;font-size:12px">Powered by Jira Service Management</p>
    </div>
    {notes_html}
  </article>"""


def _lines(text: str) -> str:
    return "<br />".join(html.escape(line) for line in text.strip().split("\n"))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What the requester gets</title>
<style>
:root {{ --text: #172B4D; --subtle: #44546F; --border: #DCDFE4; --bg: #F7F8F9; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 14px/20px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; }}
.top {{ position: sticky; top: 0; z-index: 2; background: #fff; border-bottom: 1px solid var(--border);
  padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; }}
.top h1 {{ font-size: 16px; margin: 0; }} .top span {{ color: var(--subtle); }}
.top a {{ margin-left: auto; color: #0C66E4; }}
main {{ max-width: 1280px; margin: 0 auto; padding: 16px; }}
.explain {{ background: #fff; border: 1px solid var(--border); border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; }}
.explain p {{ margin: 0 0 6px; }}
.ticket {{ margin-bottom: 28px; }}
.ticket h2 {{ font-size: 17px; margin: 0 0 10px; }} .key {{ color: #0C66E4; }}
.emails {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 480px), 1fr)); gap: 16px; }}
.mail {{ background: #fff; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
.model {{ font-size: 12px; font-weight: 700; color: var(--subtle); padding: 8px 14px 0; }}
.mail-head {{ padding: 8px 14px 10px; border-bottom: 1px solid var(--border); }}
.subject {{ font-size: 16px; font-weight: 600; margin-bottom: 6px; }}
.row {{ font-size: 13px; color: var(--subtle); }} .lbl {{ display: inline-block; width: 40px; color: #626F86; }}
/* Email body: plain Outlook rendering of the JSM notification (Calibri-like, black text) */
.mail-body {{ padding: 16px 14px; font: 15px/1.4 Calibri, Carlito, "Segoe UI", Arial, sans-serif; color: #000; }}
.mail-body p {{ margin: 0 0 12px; }}
.ok {{ margin: 0; padding: 10px 14px; background: #DCFFF1; border-top: 1px solid #7EE2B8; color: #164B35; font-size: 13px; }}
.notes {{ margin: 0; padding: 10px 14px 12px 32px; background: #FFF4E5; border-top: 1px solid #F8D4A0; color: #7A4100; font-size: 13px; }}
</style></head>
<body>
<div class="top"><h1>What the requester gets</h1><span>{stamp}</span><span>{n} tickets</span>
  <span>Preview only: nothing was sent</span><a href="{review_page}">Back to the staff review page</a></div>
<main>
<div class="explain">
  <p><strong>"Send as-is" on the review page is only a score.</strong> Nothing is sent from there.</p>
  <p>In the real flow, the bot posts the yellow internal note (staff only). A staff member then pastes the suggested
  reply into <em>Reply to customer</em>. Only that reply text reaches the requester, as this email and on the
  support portal. The triage section never leaves Jira.</p>
  <p>The orange notes under each email point out what the requester would notice if the draft went out unedited.</p>
</div>
{cards}
</main></body></html>
"""
