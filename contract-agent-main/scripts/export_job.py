"""把一份已完成的审查任务导出成自包含 HTML 报告，双击就能打开。

样式直接复用上传页面 web/index.html 里的 <style>（同一套设计系统和深浅色主题），
正文结构和类名与在线页对齐：.card / .risk / .quote / .facts / .missing / .contract。

- 静态部分（离线可用）：值得留意的条款、合同里没写的事、关键信息、
  点出处展开整条原文（全部条款文本嵌进文件）。
- 追问：内嵌 JS 连本机服务 http://127.0.0.1:8300/jobs/{id}/ask，只传问题
  （合同在服务端缓存里）。服务没开时显示启动提示，不影响其他内容。
- 合同和模型写的内容一律按纯文本转义后放进页面，不会被当成 HTML 执行。

    uv run python scripts/export_job.py 任务号 输出.html [--base http://127.0.0.1:8300]
"""

import html
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8300"
STATUS_TEXT = {"ok": "核对通过", "fixed": "改正了出处", "unverified": "未能核对"}
WEB_INDEX = Path(__file__).resolve().parents[1] / "src" / "contract_agent" / "web" / "index.html"


def fetch(path: str) -> bytes:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as resp:
        return resp.read()


def esc(text: str) -> str:
    return html.escape(str(text))


def page_css() -> str:
    """复用上传页面整套样式；多补一小段报告专属的（徽标、风险卡间距），变量同源。"""
    css = re.search(r"<style>(.*?)</style>", WEB_INDEX.read_text(encoding="utf-8"), re.S)
    extra = """
/* ---- 报告专属 ---- */
.badge { display: inline-block; border-radius: 99px; padding: 2px 12px; font-size: 14px; vertical-align: middle; }
.badge.on  { color: var(--ok);   background: color-mix(in srgb, var(--ok) 12%, transparent); }
.badge.off { color: var(--muted); background: var(--gap-soft); }
.risk header .st { font-size: 13px; color: var(--muted); margin-left: auto; flex: none; }
.risk .why, .risk .ask, .risk .not { margin: 8px 0; }
details.card > summary { list-style: none; cursor: pointer; display: flex; align-items: baseline; gap: 10px; }
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary h2 { font-size: 19px; margin: 16px 0 10px; display: flex; gap: 10px; align-items: baseline; }
details.card > summary h2 .count { font-size: 14px; font-weight: normal; color: var(--muted); }
details.card > summary::after { content: "▾ 收起"; color: var(--muted); font-size: 14px; margin-left: auto; flex: none; }
details.card:not([open]) > summary::after { content: "▸ 展开"; }
details.card > summary:hover h2 { color: var(--accent); }
.risk.safe { border-left-color: var(--ok); }
.risk.safe .rid { color: var(--ok); background: color-mix(in srgb, var(--ok) 14%, transparent); }
.risk.safe .st { color: var(--ok); }
/* ---- 关键信息区 ---- */
dl.facts dt { font-weight: 600; }
dl.facts dd { font-size: 15px; }
dl.facts .row { padding: 12px 0; }
dl.facts .srcs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 6px; }
dl.facts .srcs-label { font-size: 13px; color: var(--muted); flex: none; }
dl.facts .src, .cite .src {
  display: inline-block; padding: 2px 10px; background: var(--accent-soft); color: var(--accent);
  border-radius: 6px; text-decoration: none; font-size: 13px;
}
dl.facts .src:hover, .cite .src:hover { text-decoration: underline; text-underline-offset: 3px; background: color-mix(in srgb, var(--accent) 22%, transparent); }
"""
    return (css.group(1) if css else "") + extra


def src_button(q: dict, suffix: str = "") -> str:
    """出处按钮：显示"第 X 页 · 条款名"，点开下面展开整条（和在线页一致）。"""
    c = clause_of(q.get("clause", ""))
    if not c:
        return ""
    where = f"第 {','.join(map(str, c['pages']))} 页 · {c['heading'] or c['title'] or c['id']}{suffix}"
    return f"<button class='src' onclick='toggleClause(this,{json.dumps(q['clause'], ensure_ascii=False)})'>{esc(where)}</button>"


def dedupe_srcs(quotes: list[dict]) -> str:
    """同一条款的多条引用合并成一个出处按钮（×N 表示有几条）。"""
    order: list[str] = []
    counts: dict[str, int] = {}
    for q in quotes:
        cid = q.get("clause", "")
        if cid not in counts:
            order.append(cid)
            counts[cid] = 0
        counts[cid] += 1
    out = []
    for cid in order:
        q = next(q for q in quotes if q.get("clause") == cid)
        n = counts[cid]
        out.append(src_button(q, f" ×{n}" if n > 1 else ""))
    return "".join(out)


def safe_card(item: dict, r) -> str:
    """"没踩到的坑"卡片：样式和风险卡同款，绿色变体，附 AI 的逐条分析。"""
    rid = esc(item["rid"])
    verdict = esc(item.get("verdict"))
    out = [f'<section class="risk safe"><header><span class="rid g">{rid}</span><h3>{esc(r.title)}</h3><span class="st">{verdict}</span></header>']
    out.append(f'<p class="how">{esc(item.get("how"))}</p>')
    out.append("".join(quote_block(q) for q in item.get("quotes", [])))
    out.append(f'<p class="why"><span class="label">为什么这里不算坑：</span>{esc(item.get("why_safe"))}</p>')
    out.append(f'<p class="not"><span class="label">什么情况才算坑：</span>{esc(r.not_risk)}</p>')
    basis = esc(r.basis_shown)
    if r.status == "待补":
        basis = "（依据整理中，暂不显示）"
    out.append(f'<p class="basis"><span class="label">依据：</span>{basis}</p>')
    out.append("</section>")
    return "\n".join(out)


def quote_block(q: dict) -> str:
    return f'<div class="quote"><p>{esc(q.get("text"))}</p>{src_button(q)}</div>'


CLAUSES: list[dict] = []


def clause_of(cid: str) -> dict | None:
    return next((c for c in CLAUSES if c["id"] == cid), None)


def build_body(job: dict, labels: dict[str, str], safe_map: dict[str, dict] | None, risks_all: list) -> str:
    out: list[str] = []
    findings = (job.get("risks") or {}).get("findings", [])

    out.append('<details class="card" open><summary><h2>值得留意的条款 <span class="count">' + str(len(findings)) + "</span></h2></summary>")
    for f in findings:
        r = f.get("risk", {})
        st = r.get("status", "")
        note = ' <span class="st">依据仅供参考</span>' if st == "部分" else ""
        out.append('<section class="risk">')
        out.append(f"<header><span class=\"rid\">{esc(r.get('id'))}</span><h3>{esc(r.get('title'))}</h3>{note}</header>")
        out.append(f'<p class="how">{esc(f.get("how"))}</p>')
        out.append(f'<p class="why"><span class="label">为什么留意：</span>{esc(r.get("why"))}</p>')
        out.append(f'<p class="ask"><span class="label">可以问房东：</span>{esc(r.get("ask"))}</p>')
        out.append(f'<p class="not"><span class="label">什么情况不算：</span>{esc(r.get("not_risk"))}</p>')
        basis = esc(r.get("basis_shown"))
        if st == "部分" and "仅供参考" not in basis:
            basis += "（仅供参考：依据只覆盖一部分，各地规定可能不同）"
        out.append(f'<p class="basis"><span class="label">依据：</span>{basis}</p>')
        out.append("".join(quote_block(q) for q in f.get("quotes", [])))
        out.append("</section>")
    out.append("</details>")

    hit = {f.get("risk", {}).get("id") for f in findings}
    safe = [r for r in risks_all if r.id not in hit]
    out.append('<details class="card"><summary><h2>已核对、没踩到的坑 <span class="count">' + str(len(safe)) + "</span></h2></summary>")
    if safe_map:
        out.append('<p class="note">这些项逐条核对过，合同里没有此类安排或已有对租客合理的安排。每条附这份合同的实际写法和 AI 的判断依据。</p>')
        for r in safe:
            item = safe_map.get(r.id)
            if item:
                out.append(safe_card(item, r))
            else:
                out.append(f'<section class="risk safe"><header><span class="rid g">{esc(r.id)}</span><h3>{esc(r.title)}</h3></header><p>{esc(r.patterns)}</p></section>')
    else:
        out.append('<p class="note">这些项逐条核对过，合同里没有此类安排或已有对租客合理的安排。</p>')
        for r in safe:
            out.append(f'<section class="risk safe"><header><span class="rid g">{esc(r.id)}</span><h3>{esc(r.title)}</h3></header><p><span class="label">典型坑的写法（本合同未出现）：</span>{esc(r.patterns)}</p><p class="not"><span class="label">什么情况才算坑：</span>{esc(r.not_risk)}</p></section>')
    out.append("</details>")

    missing = job.get("missing") or []
    out.append('<section class="card"><h2>合同里没写、建议问清楚的事 <span class="count">' + str(len(missing)) + "</span></h2>")
    if missing:
        out.append('<ul class="missing">')
        for m in missing:
            r = m.get("risk", {})
            field = m.get("field", "")
            out.append(f"<li><span class=\"what\">{esc(r.get('title'))}</span>（{esc(labels.get(field, field))}）")
            out.append(f'<span class="sub">{esc(r.get("why"))}</span>')
            out.append(f'<span class="sub">可以问：{esc(r.get("ask"))}</span></li>')
        out.append("</ul>")
    else:
        out.append('<p class="empty">清单要求提示的项都写到了，没有额外要补的空白。</p>')
    out.append("</section>")

    info = (job.get("info") or {}).get("info") or {}
    out.append('<section class="card"><h2>关键信息</h2><dl class="facts">')
    for name, v in info.items():
        if not isinstance(v, dict) or "value" not in v:
            continue
        out.append(f'<div class="row"><dt>{esc(labels.get(name, name))}</dt><dd>{esc(v.get("value"))}')
        qs = v.get("quotes", [])
        if qs:
            out.append('<span class="srcs"><span class="srcs-label">出处：</span>' + dedupe_srcs(qs) + "</span>")
        out.append("</dd></div>")
    out.append("</dl></section>")

    dropped = (job.get("info") or {}).get("dropped") or []
    if dropped:
        items = "；".join(esc(d) if isinstance(d, str) else esc(f"{d.get('field')}：{d.get('reason')}") for d in dropped)
        out.append(f'<p class="empty">以下信息模型给了、但附的原文核对没通过，已扔掉（不代表合同里没写）：{items}</p>')

    out.append("""
<section class="card"><h2>接着问</h2>
<p class="note" id="ask-status">正在探测本地服务……</p>
<div class="ask-form"><input id="q" placeholder="例如：押金多久退？能养宠物吗？" onkeydown="if(event.key==='Enter')ask()"><button class="btn" id="askbtn" onclick="ask()">问</button></div>
<div id="answer"></div>
</section>""")
    return "\n".join(out)


def clauses_html(clauses: list[dict]) -> str:
    parts = [f'<h4>{esc(c["heading"] or c["title"] or c["id"])}</h4><p>{esc(c["text"])}</p>' for c in clauses]
    return "".join(parts)


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · 租房合同审查报告</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
<div class="top"><h1>__TITLE__ · 审查报告</h1><p>__META__　<span id="badge" class="badge">探测本地服务中……</span></p></div>
__BODY__
<details class="more"><summary>按条款切好的合同全文（点出处展开对应条款）</summary><div class="contract">__CLAUSES__</div></details>
<footer>不是法律意见：只提醒哪里值得留意、建议问清楚，不判断条款是否违法或无效。依据仅供自己查证。
<br>追问功能需要本地服务在运行：<code>uv run contract-agent</code>（http://127.0.0.1:8300）。</footer>
</div>
<script>
const CLAUSES = __CLAUSES_JSON__;
const BASE = "__BASE__";
const JOB = "__JOB_ID__";
const esc = (s) => { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; };
const clause = (id) => CLAUSES.find((c) => c.id === id);
const clauseName = (c) => c ? "第 " + c.pages.join("、") + " 页 · " + (c.heading || c.title || c.id) : "未知条款";

function toggleClause(btn, id) {
  let el = btn.nextElementSibling;
  if (el && el.classList && el.classList.contains("clause-full")) { el.remove(); return; }
  const c = clause(id);
  el = document.createElement("div");
  el.className = "clause-full";
  el.innerHTML = c ? "<div class=\\"head\\">" + esc(clauseName(c)) + "</div><div class=\\"text\\">" + esc(c.text) + "</div>"
                   : "<div class=\\"head\\">" + esc(id) + "（报告里没有这一条）</div>";
  btn.after(el);
}

const badgeEl = document.getElementById("badge");
const askStatusEl = document.getElementById("ask-status");
const askBtnEl = document.getElementById("askbtn");

fetch(BASE + "/jobs/" + JOB).then((r) => { if (!r.ok) throw 0; return r.json(); })
  .then(() => { badgeEl.textContent = "追问可用"; badgeEl.className = "badge on";
                askStatusEl.textContent = "想问什么就问，回答只根据这份合同，说到合同的地方附出处。"; })
  .catch(() => { badgeEl.textContent = "离线模式 · 追问不可用"; badgeEl.className = "badge off";
                 askStatusEl.textContent = "本地服务没有在运行。要启用追问：运行 uv run contract-agent 后刷新本页。"; });

async function ask() {
  const q = document.getElementById("q").value.trim();
  if (!q) return;
  const ans = document.getElementById("answer");
  ans.innerHTML = '<p class="question">' + esc(q) + "</p>";
  let box = document.createElement("div");
  box.className = "answer";
  ans.appendChild(box);
  askBtnEl.disabled = true;
  try {
    const resp = await fetch(BASE + "/jobs/" + JOB + "/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", text = "";
    const render = (items) => {
      for (const c of items) {
        const el = document.createElement("span");
        el.className = "cite " + c.status;
        el.innerHTML = "<q>" + esc(c.text) + "</q>";
        const b = document.createElement("button");
        b.className = "src";
        b.textContent = clauseName(clause(c.clause));
        b.onclick = () => toggleClause(b, c.clause);
        el.appendChild(b);
        const mark = { ok: "", fixed: "（改正了出处）", unverified: "（未能核对）" }[c.status];
        if (mark) { const m = document.createElement("span"); m.className = "answer-note" + (c.status === "unverified" ? " bad" : ""); m.textContent = mark; el.appendChild(m); }
        box.appendChild(el);
      }
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === "delta") { text += ev.text; box.textContent = text; }
        else if (ev.type === "citations") { box.textContent = text; render(ev.items); box = document.createElement("div"); box.className = "answer"; ans.appendChild(box); }
        else if (ev.type === "error") { const p = document.createElement("p"); p.className = "answer-note bad"; p.textContent = "出错：" + ev.message; ans.appendChild(p); }
      }
    }
  } catch (e) {
    const p = document.createElement("p");
    p.className = "answer-note bad";
    p.textContent = "追问失败：" + e.message + "（本地服务在运行吗？）";
    ans.appendChild(p);
  } finally { askBtnEl.disabled = false; }
}
</script>
</body>
</html>
"""


def main() -> None:
    global BASE, CLAUSES
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--base=")), BASE)
    BASE = base.rstrip("/")
    job_id, out = args[0], Path(args[1])
    job = json.loads(fetch(f"/jobs/{job_id}"))
    labels = {f["name"]: f["label"] for f in json.loads(fetch("/fields"))}

    CLAUSES = job.get("clauses") or []
    name = Path(job.get("filename") or job_id).stem
    from contract_agent.checklist import load
    risks_all = load()
    safe_path = out.parent / "_安全分析" / f"{job_id}.json"
    safe_map = None
    if safe_path.exists():
        safe_map = {i["rid"]: i for i in json.loads(safe_path.read_text(encoding="utf-8"))["items"]}
    meta = f"{esc(job.get('filename'))} · {len(CLAUSES)} 个条款 · 任务号 {esc(job_id)}"
    findings = (job.get("risks") or {}).get("findings", [])
    page = (TEMPLATE
            .replace("__TITLE__", esc(name))
            .replace("__META__", meta)
            .replace("__CSS__", page_css().replace("</", "<\\/"))
            .replace("__BODY__", build_body(job, labels, safe_map, risks_all))
            .replace("__CLAUSES__", clauses_html(CLAUSES))
            .replace("__CLAUSES_JSON__", json.dumps(CLAUSES, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__JOB_ID__", esc(job_id))
            .replace("__BASE__", esc(BASE)))
    out.write_text(page, encoding="utf-8")
    print(f"{out.name}: {len(page)} 字符，{len(CLAUSES)} 条款，{len(findings)} 条风险，{len(risks_all) - len(findings)} 条没踩到，AI 分析 {'有' if safe_map else '无（通用文案）'}")


if __name__ == "__main__":
    main()
