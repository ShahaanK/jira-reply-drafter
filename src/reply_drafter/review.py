"""Local HTML review page: each ticket with the exact internal note the bot would post.

Written to out/ (gitignored; it contains real ticket text). Scores are kept in
the browser and exported with the "Download scores" button as JSON, which
scripts/run_bot.py --summarize reads back.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from reply_drafter.drafter import DraftResult
from reply_drafter.jira_note import adf_to_html, build_payload

JIRA_BROWSE = "https://jira.example.edu/browse/"
# Lock glyph Jira shows after the author's name on internal notes.
_LOCK_SVG = (
    '<svg class="lock" viewBox="0 0 16 16" width="12" height="12" role="img" aria-label="Internal note">'
    '<path fill="none" stroke="currentColor" stroke-width="1.5" d="M4.75 7V5a3.25 3.25 0 0 1 6.5 0v2"/>'
    '<rect x="3" y="7" width="10" height="7.25" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'
)
SCORES = [
    ("send", "Send as-is"),
    ("minor", "Minor edits"),
    ("major", "Major edits"),
    ("wrong", "Wrong / unusable"),
]


def write_review_page(
    tickets: list[tuple[DraftResult, ...]],
    *,
    out_dir: Path,
    mode: str,
    scrubbed: bool,
    operator: str,
    path: Path | None = None,
) -> Path:
    """path: overwrite an existing page (keeps the browser's saved scores, which are keyed by file name)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = path or out_dir / f"review-{mode}-{stamp}.html"
    cards = "\n".join(_ticket_card(drafts, operator, mode) for drafts in tickets)
    n_drafts = sum(len(d) for d in tickets)
    n_flags = sum(1 for d in tickets for r in d if r.flags or r.error)
    models = sorted({r.llm.model for d in tickets for r in d if r.llm})
    page = _PAGE.format(
        title=f"HELP bot review - {mode}",
        mode=html.escape(mode),
        stamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        n_tickets=len(tickets),
        n_drafts=n_drafts,
        n_flags=n_flags,
        models=html.escape(", ".join(models) or "none"),
        scrub="on" if scrubbed else "off",
        cards=cards,
        file_stem=path.stem,
        requester_page=html.escape(requester_page_name(path)),
    )
    path.write_text(page, encoding="utf-8")
    return path


def requester_page_name(review_path: Path) -> str:
    """review-eval-20261019-1403.html -> requester-eval-20261019-1403.html"""
    return review_path.name.replace("review-", "requester-", 1)


def _ticket_card(drafts: tuple[DraftResult, ...], operator: str, mode: str) -> str:
    first = drafts[0]
    key = html.escape(first.target_key)
    blocks = "\n".join(_draft_block(r, operator, mode) for r in drafts)
    historical = ""
    if mode == "eval" and first.historical_reply:
        historical = (
            '<details class="hist" open><summary>What staff actually replied '
            "(may be outdated: policies change)</summary>"
            f"<pre>{html.escape(first.historical_reply)}</pre></details>"
        )
    return f"""
<section class="ticket" id="{key}">
  <header class="t-head">
    <a class="t-key" href="{JIRA_BROWSE}{key}" target="_blank" rel="noopener">{key}</a>
    <h2>{html.escape(first.summary)}</h2>
    <span class="t-date">Created {html.escape(first.created)}</span>
  </header>
  <details class="request" open><summary>Request</summary><pre>{html.escape(first.question)}</pre></details>
  {historical}
  {blocks}
</section>"""


def _draft_block(r: DraftResult, operator: str, mode: str) -> str:
    model = r.llm.model if r.llm else "no model"
    sid = html.escape(f"{r.target_key}|{model}")
    if r.error and not r.reply:
        return f'<div class="draft"><div class="error">Draft failed ({html.escape(model)}): {html.escape(r.error)}</div></div>'

    payload = build_payload(r)
    note_html = adf_to_html(payload["body"])
    flags = "".join(f"<li>{html.escape(f)}</li>" for f in r.flags)
    flags_html = f'<ul class="flags">{flags}</ul>' if flags else '<p class="ok">No flags</p>'
    exemplars = "".join(
        f'<li><a href="{JIRA_BROWSE}{html.escape(h.key)}" target="_blank" rel="noopener">{html.escape(h.key)}</a>'
        f' <span class="muted">{html.escape(h.created)}</span> {html.escape(h.summary[:70])}</li>'
        for h in r.exemplars
    )
    kb = ", ".join(html.escape(a.slug) for a in r.kb_candidates) or "none"
    redactions = ", ".join(f"{k} {v}" for k, v in (r.llm.redactions if r.llm else {}).items()) or "none"
    sent = html.escape(r.llm.sent_prompt) if r.llm else ""
    radios = "".join(
        f'<label><input type="radio" name="{sid}" value="{v}"> {t}</label>' for v, t in SCORES
    )
    return f"""
  <div class="draft">
    <div class="jira">
      <div class="jira-h">Activity</div>
      <div class="jira-tabs"><span>All</span><span class="on">Comments</span><span>History</span><span>Work log</span><span>Approvals</span></div>
      <div class="comment internal">
        <div class="avatar">{html.escape(_initials(operator))}</div>
        <div class="c-main">
          <div class="c-name">{html.escape(operator)} {_LOCK_SVG}</div>
          <div class="c-time">just now (preview)</div>
          <div class="c-body">{note_html}</div>
        </div>
      </div>
    </div>
    <aside class="review">
      <div class="model">{html.escape(model)}</div>
      <div class="score" data-id="{sid}">{radios}
        <textarea placeholder="What would you change?" data-note="{sid}"></textarea></div>
      <h3>Checks</h3>{flags_html}
      <h3>Triage</h3><p>{html.escape(r.category)} - {html.escape(r.confidence)}{' - needs human' if r.needs_human else ''}</p>
      <p class="muted">KB candidates: {kb}</p>
      <h3>Grounded on</h3><ol class="ex">{exemplars}</ol>
      <details><summary>Exactly what was sent to the model (redactions: {html.escape(redactions)})</summary><pre>{sent}</pre></details>
      <details><summary>Jira payload (JSON)</summary><pre>{html.escape(json.dumps(payload, indent=2))}</pre></details>
    </aside>
  </div>"""


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --text: #172B4D; --subtle: #44546F; --border: #DCDFE4; --bg: #F7F8F9; --card: #FFFFFF;
  --link: #0C66E4;
  --bad: #AE2E24; --bad-bg: #FFECEB; --good: #216E4E;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 14px/20px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; }}
.top {{ position: sticky; top: 0; z-index: 2; background: var(--card); border-bottom: 1px solid var(--border);
  padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; }}
.top h1 {{ font-size: 16px; margin: 0; }}
.top span {{ color: var(--subtle); }}
.top .req {{ margin-left: auto; color: var(--link); font-weight: 600; }}
.top button {{ background: var(--link); color: #fff; border: 0; border-radius: 3px;
  padding: 6px 12px; font: inherit; cursor: pointer; }}
main {{ max-width: 1280px; margin: 0 auto; padding: 16px; }}
.ticket {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px; padding: 16px; margin-bottom: 20px; }}
.t-head {{ display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; }}
.t-head h2 {{ font-size: 18px; margin: 0; flex: 1 1 300px; }}
.t-key {{ color: var(--link); font-weight: 600; text-decoration: none; }}
.t-date, .muted {{ color: var(--subtle); font-size: 12px; }}
details {{ margin: 8px 0; }}
summary {{ cursor: pointer; color: var(--subtle); font-weight: 600; }}
pre {{ white-space: pre-wrap; word-break: break-word; background: var(--bg); border-radius: 3px; padding: 8px;
  margin: 6px 0 0; font: 13px/19px ui-monospace, SFMono-Regular, Consolas, monospace; max-height: 320px; overflow: auto; }}
.hist pre {{ background: #F1F2F4; }}
.draft {{ display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px; margin-top: 14px;
  border-top: 1px solid var(--border); padding-top: 14px; }}
@media (max-width: 860px) {{ .draft {{ grid-template-columns: 1fr; }} }}
/* Jira activity panel styling */
.jira {{ --j-text: #292A2E; --j-subtle: #6B6E76; --j-link: #1868DB;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--j-text); }}
.jira-h {{ font-size: 16px; font-weight: 653; margin-bottom: 8px; }}
.jira-tabs {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 16px; }}
.jira-tabs span {{ font-size: 13.33px; font-weight: 500; line-height: 30px; padding: 0 12px; border-radius: 4px;
  border: 1px solid transparent; color: #505258; }}
.jira-tabs span.on {{ color: var(--j-link); background: #E9F2FE; border-color: var(--j-link); }}
.comment {{ display: flex; gap: 10px; padding: 12px; }}
.comment.internal {{ background: #FEF7C8; }}
.avatar {{ flex: 0 0 32px; height: 32px; margin: 6px 0 0 2px; border-radius: 9999px; background: #1868DB; color: #fff;
  display: grid; place-items: center; font-weight: 653; font-size: 12px; }}
.c-main {{ flex: 1; min-width: 0; padding-top: 5px; }}
.c-name {{ font-size: 14px; font-weight: 653; line-height: 17px; display: flex; align-items: center; gap: 12px; }}
.c-name .lock {{ color: var(--j-text); flex: 0 0 12px; }}
.c-time {{ font-size: 12px; line-height: 16px; color: var(--j-subtle); margin-top: 2px; }}
.c-body {{ margin-top: 13px; font-size: 14px; line-height: 24px; }}
.c-body p {{ margin: 0 0 12px; }} .c-body p:last-child {{ margin-bottom: 0; }}
.c-body hr {{ border: 0; border-top: 1px solid rgba(11, 18, 14, 0.14); margin: 12px 0; }}
.c-body a {{ color: var(--j-link); }}
.review h3 {{ font-size: 12px; text-transform: uppercase; color: var(--subtle); margin: 14px 0 4px; }}
.review p {{ margin: 0 0 4px; }}
.model {{ font-weight: 700; }}
.score {{ display: grid; gap: 4px; margin-top: 8px; padding: 8px; border: 1px solid var(--border); border-radius: 3px; }}
.score textarea {{ width: 100%; min-height: 54px; font: inherit; border: 1px solid var(--border); border-radius: 3px; padding: 4px; }}
.flags {{ margin: 0; padding-left: 18px; color: var(--bad); }}
.ok {{ color: var(--good); }}
.error {{ background: var(--bad-bg); color: var(--bad); padding: 8px; border-radius: 3px; }}
.ex {{ margin: 0; padding-left: 20px; }} .ex a {{ color: var(--link); }}
</style></head>
<body>
<div class="top">
  <h1>HELP bot review: {mode}</h1>
  <span>{stamp}</span><span>{n_tickets} tickets, {n_drafts} drafts</span>
  <span>{n_flags} with flags</span><span>Models: {models}</span><span>PII scrub: {scrub}</span>
  <span>Dry run: nothing was posted to Jira</span>
  <a class="req" href="{requester_page}">What the requester gets</a>
  <button type="button" id="dl">Download scores</button>
</div>
<main>
{cards}
</main>
<script>
(function () {{
  var KEY = "help-review-{file_stem}";
  var state = {{}};
  try {{ state = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{ state = {{}}; }}
  function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(state)); }} catch (e) {{}} }}
  document.querySelectorAll(".score").forEach(function (box) {{
    var id = box.dataset.id, s = state[id] || {{}};
    box.querySelectorAll("input").forEach(function (r) {{
      if (r.value === s.score) r.checked = true;
      r.addEventListener("change", function () {{ (state[id] = state[id] || {{}}).score = r.value; save(); }});
    }});
    var ta = box.querySelector("textarea");
    ta.value = s.note || "";
    ta.addEventListener("input", function () {{ (state[id] = state[id] || {{}}).note = ta.value; save(); }});
  }});
  document.getElementById("dl").addEventListener("click", function () {{
    var rows = Object.keys(state).map(function (id) {{
      var p = id.split("|"); return {{ ticket: p[0], model: p[1], score: state[id].score || "", note: state[id].note || "" }};
    }});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(rows, null, 2)], {{ type: "application/json" }}));
    a.download = "scores-{file_stem}.json"; a.click();
  }});
}})();
</script>
</body></html>
"""
