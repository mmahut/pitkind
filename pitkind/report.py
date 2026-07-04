"""Render a canonical output file as a self-contained HTML deliberation report.

Read-only presentation layer: consumes a validated CanonicalFile dict and
produces one HTML file with no external dependencies. It never modifies or
reinterprets the record — failures are shown as failures.
"""

from __future__ import annotations

import json
from typing import Any

_ROUND_TITLES = {
    "R0": ("R0", "Independent review"),
    "R1": ("R1", "Deliberation 1"),
    "R2": ("R2", "Final ballot"),
}


def render_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Build CIP-136 vote metadata from the final committee outcome."""
    summary = data.get("summary")
    verdict = data.get("official_verdict")
    if data.get("run_status") != "complete" or not summary or not verdict:
        raise ValueError("metadata requires a complete run with a summary")
    rationale = next(
        entry["output"]["rationale"]
        for entry in data["stages"]["R2"]
        if entry["output"].get("verdict") == verdict
    )
    action_id = data["proposal"]["id"]
    stance = {"YES": "constitutional", "NO": "unconstitutional", "ABSTAIN": "undetermined"}[verdict]
    return {
        "@context": {
            "@language": "en",
            "CIP100": "https://github.com/cardano-foundation/CIPs/blob/master/CIP-0100/README.md#",
            "CIP129": "https://github.com/cardano-foundation/CIPs/blob/master/CIP-0129/README.md#",
            "CIP136": "https://github.com/cardano-foundation/CIPs/blob/master/CIP-0136/README.md#",
            "hashAlgorithm": "CIP100:hashAlgorithm",
            "body": {"@id": "CIP136:body", "@context": {
                "govActionId": "CIP129:governance-action-identifiers",
                "summary": "CIP136:summary",
                "rationaleStatement": "CIP136:rationaleStatement",
                "conclusion": "CIP136:conclusion",
                "references": {"@id": "CIP100:references", "@container": "@set", "@context": {
                    "Other": "CIP100:OtherReference",
                    "label": "CIP100:reference-label",
                    "uri": "CIP100:reference-uri",
                }},
            }},
            "authors": {"@id": "CIP100:authors", "@container": "@set", "@context": {
                "name": "http://xmlns.com/foaf/0.1/name",
            }},
        },
        "hashAlgorithm": "blake2b-256",
        "body": {
            "govActionId": action_id,
            "summary": summary,
            "rationaleStatement": rationale,
            "conclusion": f"Pitkind finds this governance action {stance}.",
            "references": [{
                "@type": "Other",
                "label": "Full deliberation report",
                "uri": f"https://deliberative.cc/{action_id}",
            }],
        },
        "authors": [{"name": "Pitkind"}],
    }


def _slim(data: dict[str, Any]) -> dict[str, Any]:
    """Extract what the report needs; drop the bulky user_message transcripts."""
    stages = {}
    for name in ("R0", "R1", "R2"):
        entries = data["stages"].get(name)
        if entries is None:
            continue
        slim_entries = []
        for e in entries:
            out = e["output"]
            if "verdict" in out:
                slim_entries.append({
                    "model": e["model"],
                    "ok": True,
                    "verdict": out["verdict"],
                    "confidence": out["confidence"],
                    "rationale": out["rationale"],
                    "decisive_provision": out["decisive_provision"],
                    "engaged_provisions": out["engaged_provisions"],
                    "argument_responses": out.get("argument_responses", []),
                    "strongest_counterargument": out.get("strongest_counterargument", ""),
                    "revision_reason": out.get("revision_reason", ""),
                    "missing_information": out.get("missing_information", []),
                })
            else:
                slim_entries.append({
                    "model": e["model"],
                    "ok": False,
                    "status": out["status"],
                    "error": out.get("error"),
                    "raw": out.get("raw"),
                })
        stages[name] = slim_entries
    costs = [
        e["usage"]["cost_usd"]
        for entries in data["stages"].values()
        for e in entries
        if e.get("usage") and e["usage"].get("cost_usd") is not None
    ]
    return {
        "pitkind_version": data.get("pitkind_version", "v0.1"),
        "proposal": {
            "id": data["proposal"].get("id", "unknown"),
            "title": data["proposal"].get("title", ""),
            "type": data["proposal"].get("type", ""),
        },
        "cost_usd": round(sum(costs), 4) if costs else None,
        "run_status": data["run_status"],
        "official_verdict": data["official_verdict"],
        "summary": data.get("summary"),
        "review_arguments": data.get("review_arguments", {}),
        "proposal_sha256": data["config_fingerprint"].get("proposal_sha256", ""),
        "timestamps": data["timestamps"],
        "rounds": [
            {"key": k, "label": _ROUND_TITLES[k][0], "sub": _ROUND_TITLES[k][1]}
            for k in ("R0", "R1", "R2")
        ],
        "stages": stages,
        "aggregations": data["aggregations"],
        "models": [m["openrouter_id"] for m in data["config_fingerprint"]["models"]],
    }


def render_report(data: dict[str, Any]) -> str:
    payload = json.dumps(_slim(data), ensure_ascii=False)
    title = data["proposal"].get("title") or data["proposal"].get("id", "Deliberation")
    return (
        _TEMPLATE
        .replace("__TITLE__", title.replace("<", "&lt;").replace(">", "&gt;"))
        # Escape every '<': closing tags can end the block, while '<!--<script>'
        # can enter HTML's double-escaped script state and swallow its closing tag.
        .replace("__DATA__", payload.replace("<", "\\u003c"))
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deliberation — __TITLE__</title>
<style>
  :root {
    --page:        #f6f5f1;
    --surface:     #ffffff;
    --ink:         #17160f;
    --ink-2:       #54524a;
    --muted:       #8d8a7f;
    --grid:        #e8e6de;
    --baseline:    #c6c4b8;
    --border:      rgba(23,22,15,0.08);
    --yes:         #2a78d6;
    --no:          #e34948;
    --neutral:     #f1f0ec;
    --good:        #0ca30c;
    --good-text:   #1c7a1c;
    --critical:    #d03b3b;
    --shadow:      0 1px 2px rgba(23,22,15,.05), 0 10px 28px -10px rgba(23,22,15,.10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --page:        #101010;
      --surface:     #1b1b19;
      --ink:         #f4f3ef;
      --ink-2:       #c3c2b7;
      --muted:       #8d8a7f;
      --grid:        #2e2e2b;
      --baseline:    #3b3b37;
      --border:      rgba(255,255,255,0.09);
      --yes:         #4a92e8;
      --no:          #e87070;
      --neutral:     #34342f;
      --good:        #2fbf2f;
      --good-text:   #4fca4f;
      --critical:    #e06060;
      --shadow:      0 1px 2px rgba(0,0,0,.4), 0 12px 32px -12px rgba(0,0,0,.6);
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background:
      radial-gradient(1100px 420px at 50% -180px,
        color-mix(in srgb, var(--yes) 6%, transparent), transparent 70%),
      var(--page);
    color: var(--ink);
    font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
    padding: 32px 18px 72px;
  }
  .wrap { max-width: 1160px; margin: 0 auto; }

  header { margin-bottom: 24px; }
  .kicker {
    display: inline-flex; align-items: center; gap: 8px;
    color: var(--muted); font-size: 11px; font-weight: 650;
    letter-spacing: .12em; text-transform: uppercase;
  }
  .kicker::before { content: ""; width: 22px; height: 1.5px; background: var(--baseline); }
  h1 {
    font-size: clamp(20px, 3.2vw, 27px); font-weight: 700;
    letter-spacing: -0.015em; line-height: 1.25;
    margin: 6px 0 14px; max-width: 60ch; text-wrap: balance;
  }
  .headline { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 12px; }
  .verdict-hero {
    display: inline-flex; align-items: center; gap: 9px;
    padding: 9px 18px; border-radius: 999px;
    font-size: 16px; font-weight: 700; letter-spacing: -0.01em;
    border: 1px solid color-mix(in srgb, var(--vc, var(--baseline)) 45%, transparent);
    background: linear-gradient(180deg,
      color-mix(in srgb, var(--vc, var(--baseline)) 17%, var(--surface)),
      color-mix(in srgb, var(--vc, var(--baseline)) 8%, var(--surface)));
    box-shadow: 0 6px 20px -8px color-mix(in srgb, var(--vc, var(--baseline)) 55%, transparent);
  }
  .verdict-hero .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--vc, var(--muted));
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--vc, var(--muted)) 22%, transparent);
  }
  .meta { color: var(--ink-2); font-size: 13px; }
  .fineprint { color: var(--muted); font-size: 12px; max-width: 78ch; margin-top: 8px; }
  #review-flags:empty { display: none; }
  #review-flags { margin-top: 8px; }
  #outcome-summary { max-width: 78ch; margin: 12px 0 0; color: var(--ink-2); line-height: 1.55; }
  #outcome-summary:empty { display: none; }
  .flag-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 2px 10px; margin: 0 6px 4px 0; border-radius: 999px;
    font-size: 12px; font-weight: 600; color: var(--ink-2);
    border: 1px solid var(--grid); background: var(--neutral);
  }

  .layout { display: grid; gap: 18px; grid-template-columns: 1fr; }
  @media (min-width: 1000px) { .layout { grid-template-columns: minmax(0,1fr) 360px; align-items: start; } }

  .panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; padding: 18px 20px;
    box-shadow: var(--shadow);
  }
  .panel-title { font-size: 13.5px; font-weight: 700; letter-spacing: -0.005em; }
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: var(--ink-2); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .legend i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

  .flow-scroll { overflow-x: auto; }
  .flow { position: relative; min-width: 620px; }
  .flow svg { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .grid {
    position: relative; display: grid; gap: 14px 28px; align-items: center;
    grid-template-columns: minmax(150px, max-content) repeat(3, minmax(110px, 1fr));
  }
  .col-head { text-align: center; padding-bottom: 4px; }
  .col-head .r { font-weight: 700; font-size: 13px; }
  .col-head .s { color: var(--muted); font-size: 11px; letter-spacing: .02em; }
  .row-label { font-size: 13px; color: var(--muted); white-space: nowrap; line-height: 1.35; }
  .row-label b { color: var(--ink); font-weight: 600; }

  .node {
    position: relative; z-index: 1;
    display: flex; flex-direction: column; align-items: center; gap: 1px;
    width: 100%; padding: 8px 10px; border-radius: 12px; cursor: pointer;
    font: inherit; color: var(--ink); text-align: center;
    border: 1.5px solid color-mix(in srgb, var(--vc) 65%, transparent);
    background: linear-gradient(180deg,
      color-mix(in srgb, var(--vc) 14%, var(--surface)),
      color-mix(in srgb, var(--vc) 7%, var(--surface)));
    transition: box-shadow .15s ease, transform .15s ease;
  }
  .node .v { display: inline-flex; align-items: center; gap: 6px; font-weight: 700; font-size: 13px; }
  .node .v i { width: 8px; height: 8px; border-radius: 50%; background: var(--vc); }
  .node .c { font-size: 11px; color: var(--muted); }
  .node:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 16px -6px color-mix(in srgb, var(--vc) 55%, transparent);
  }
  .node.sel { box-shadow: 0 0 0 2px var(--surface), 0 0 0 4px var(--vc); }
  .node.fail { border-style: dashed; --vc: var(--muted); background: var(--neutral); }
  .node:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
  .placeholder { text-align: center; color: var(--muted); font-size: 12px; border: 1px dashed var(--grid); border-radius: 12px; padding: 10px 6px; }

  .agg-label { font-size: 11px; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
  .agg {
    display: flex; justify-content: center; align-items: baseline; gap: 6px;
    padding: 8px; border-top: 2px solid var(--grid);
    font-size: 13px; font-weight: 700;
  }
  .agg .tally { font-weight: 400; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }

  #tooltip {
    position: fixed; z-index: 10; max-width: 300px; pointer-events: none;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 9px 12px; font-size: 12px; line-height: 1.5; color: var(--ink-2);
    box-shadow: 0 6px 24px -4px rgba(0,0,0,.22); display: none;
  }
  #tooltip b { color: var(--ink); }

  .detail h2 { font-size: 14px; margin-bottom: 2px; overflow-wrap: anywhere; }
  .detail .sub { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
  .detail .empty { color: var(--muted); font-size: 13px; padding: 26px 8px; text-align: center; }
  @media (min-width: 1000px) {
    .detail { position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto; }
  }
  .chip {
    display: inline-flex; align-items: center; gap: 7px; padding: 4px 12px;
    border-radius: 999px; font-size: 12px; font-weight: 700;
    border: 1px solid color-mix(in srgb, var(--vc) 45%, transparent);
    background: color-mix(in srgb, var(--vc) 11%, var(--surface));
  }
  .chip i { width: 8px; height: 8px; border-radius: 50%; background: var(--vc); }
  .kv { margin: 14px 0 4px; font-size: 11px; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; }
  .prose { font-size: 13px; color: var(--ink-2); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .prov { border-top: 1px solid var(--grid); padding: 10px 0; }
  .prov .p-head { display: flex; justify-content: space-between; gap: 8px; font-size: 12.5px; font-weight: 600; }
  .assess { font-size: 11px; font-weight: 700; white-space: nowrap; }
  .assess.satisfied { color: var(--good-text); }
  .assess.violated { color: var(--critical); }
  .assess.not_applicable, .assess.cannot_determine { color: var(--muted); }
  .prov .req { color: var(--muted); font-size: 12px; font-style: italic; margin: 2px 0; }
  .prov .rea { color: var(--ink-2); font-size: 12px; }
  pre.raw { background: var(--neutral); border-radius: 10px; padding: 10px; overflow-x: auto; font-size: 11px; max-height: 220px; }

  details.section {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; box-shadow: var(--shadow);
    margin-top: 18px; overflow: hidden;
  }
  details.section > summary {
    cursor: pointer; list-style: none; user-select: none;
    display: flex; align-items: center; gap: 11px;
    padding: 15px 20px; font-size: 13.5px; font-weight: 700; color: var(--ink);
  }
  details.section > summary::-webkit-details-marker { display: none; }
  details.section > summary::before {
    content: ""; flex: none; width: 7px; height: 7px;
    border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted);
    transform: rotate(-45deg); transition: transform .15s ease;
  }
  details.section[open] > summary::before { transform: rotate(45deg); }
  details.section > summary:hover { background: color-mix(in srgb, var(--ink) 3%, transparent); }
  details.section > summary .count { font-weight: 400; color: var(--muted); font-size: 12.5px; }
  details.section > .section-body { padding: 2px 20px 20px; }

  #arguments .arg-card {
    border: 1px solid var(--grid); border-radius: 12px;
    padding: 13px 16px; margin-top: 12px;
    background: color-mix(in srgb, var(--page) 45%, var(--surface));
  }
  #arguments .arg-card > b { font-size: 13px; }
  #arguments .arg-card .prose { margin-top: 4px; }
  #arguments .arg-card .meta { margin-top: 6px; font-size: 12px; }

  .tbl-scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  th {
    color: var(--muted); font-weight: 650; font-size: 11px;
    text-transform: uppercase; letter-spacing: .07em; white-space: nowrap;
  }
  tbody tr:last-child td, tr:last-child td { border-bottom: none; }
  tr:hover td { background: color-mix(in srgb, var(--ink) 2.5%, transparent); }
  td.seat { font-weight: 600; overflow-wrap: anywhere; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 2px 10px; border-radius: 999px; white-space: nowrap;
    font-size: 12px; font-weight: 700;
    border: 1px solid color-mix(in srgb, var(--vc) 40%, transparent);
    background: color-mix(in srgb, var(--vc) 10%, var(--surface));
  }
  .badge i { width: 7px; height: 7px; border-radius: 50%; background: var(--vc); flex: none; }
  .badge .bc { font-weight: 400; color: var(--muted); }
  .badge.fail { --vc: var(--muted); border-style: dashed; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">pitkind · constitutional deliberation</div>
    <h1 id="h-title"></h1>
    <div class="headline">
      <span class="verdict-hero" id="hero"></span>
      <span class="meta" id="h-meta"></span>
    </div>
    <p id="outcome-summary"></p>
    <p id="review-flags"></p>
  </header>

  <div class="layout">
    <section class="panel">
      <div class="chart-head">
        <div>
          <div class="panel-title">Deliberation flow</div>
          <div class="meta">Each row is one committee seat; click a verdict for its full reasoning.</div>
        </div>
        <div class="legend">
          <span><i style="background:var(--yes)"></i>YES — compliant</span>
          <span><i style="background:var(--no)"></i>NO — non-compliant</span>
          <span><i style="background:var(--muted)"></i>ABSTAIN — decisive information missing</span>
          <span><i style="background:var(--muted)"></i>failed</span>
        </div>
      </div>
      <div class="flow-scroll">
        <div class="flow" id="flow">
          <svg id="edges" aria-hidden="true"></svg>
          <div class="grid" id="grid"></div>
        </div>
      </div>
    </section>

    <aside class="panel detail" id="detail" aria-live="polite">
      <div class="empty">Click a verdict node to read the model’s reasoning.</div>
    </aside>
  </div>

  <details class="section tableview">
    <summary>Table view <span class="count">all rounds, all seats</span></summary>
    <div class="section-body tbl-scroll"><table id="tbl"></table></div>
  </details>

  <details class="section argview">
    <summary>Recorded constitutional arguments and missing evidence <span class="count" id="arg-count"></span></summary>
    <div class="section-body" id="arguments"></div>
  </details>
</div>

<div id="tooltip"></div>

<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  const data = JSON.parse(document.getElementById("data").textContent);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = id => id.includes("/") ? id.split("/")[1] : id;
  const ballotColor = v => v === "YES" ? "var(--yes)" : v === "NO" ? "var(--no)" : "var(--muted)";
  const vcolor = e => e.ok ? ballotColor(e.verdict) : "var(--muted)";

  /* header */
  document.getElementById("h-title").textContent =
    data.proposal.title || data.proposal.id;
  const hero = document.getElementById("hero");
  if (data.run_status === "complete") {
    hero.style.setProperty("--vc", ballotColor(data.official_verdict));
    hero.innerHTML = '<span class="dot"></span>Committee ballot: ' + esc(data.official_verdict);
  } else {
    hero.innerHTML = '<span class="dot"></span>Run failed — no verdict';
  }
  document.getElementById("h-meta").textContent =
    data.pitkind_version + " · " + data.proposal.id + (data.proposal.type ? " · " + data.proposal.type : "") +
    " · " + data.models.length + " seats · " + (data.timestamps.started_at || "").slice(0, 10) +
    (data.cost_usd != null ? " · $" + data.cost_usd.toFixed(2) : "");
  document.getElementById("outcome-summary").textContent = data.summary || "";
  const lastRound = data.stages.R2 ? "R2" : data.stages.R1 ? "R1" : "R0";
  const flags = data.aggregations[lastRound]?.review_flags || [];
  document.getElementById("review-flags").innerHTML = flags.length
    ? flags.map(f => '<span class="flag-chip">⚑ ' + esc(f.replaceAll("_", " ")) + "</span>").join("") : "";

  /* recorded arguments & missing evidence (bottom section) */
  let argumentsHtml = "";
  let argCount = 0;
  for (const [id, argument] of Object.entries(data.review_arguments)) {
    argCount++;
    argumentsHtml += '<div class="arg-card"><b>' + esc(id) + " · " + esc(argument.provision) + "</b> — " +
      '<span class="assess ' + esc(argument.assessment) + '">' + esc(argument.assessment) + "</span>" +
      '<div class="prose">' + esc(argument.requirement) + "</div>" +
      '<div class="prose">' + esc(argument.reasoning) + '</div><div class="meta">Evidence: ' + esc(argument.evidence) + "</div>";
    const responses = (data.stages[lastRound] || []).flatMap(e =>
      (e.argument_responses || []).filter(r => r.argument_id === id));
    if (responses.some(r => r.assessment === "unresolved")) argumentsHtml += '<div class="meta">Unresolved in latest round</div>';
    if (!responses.length) argumentsHtml += '<div class="meta">Not yet answered in a later round</div>';
    argumentsHtml += "</div>";
  }
  for (const e of data.stages[lastRound] || []) {
    for (const item of e.missing_information || []) {
      argCount++;
      argumentsHtml += '<div class="arg-card"><b>Missing information · ' + esc(e.model) + "</b>" +
        '<div class="prose">' + esc(item.provision) + ": " + esc(item.question) + "</div>" +
        '<div class="prose">Why decisive: ' + esc(item.why_decisive) + "</div>" +
        '<div class="prose">If true: ' + esc(item.if_true) + "</div>" +
        '<div class="prose">If false: ' + esc(item.if_false) + "</div>" +
        '<div class="meta">Needed in next retrieval: ' + esc(item.source) + "</div></div>";
    }
  }
  if (data.proposal_sha256) argumentsHtml += '<p class="meta" style="margin-top:12px">Snapshot SHA-256: <span class="mono">' + esc(data.proposal_sha256) + "</span></p>";
  document.getElementById("arguments").innerHTML = argumentsHtml || '<p class="meta" style="margin-top:10px">No structured argument catalog in this record.</p>';
  document.getElementById("arg-count").textContent = argCount ? argCount + (argCount === 1 ? " item" : " items") : "";

  /* flow grid */
  const grid = document.getElementById("grid");
  let cells = "<div></div>";
  for (const r of data.rounds) {
    cells += '<div class="col-head"><div class="r">' + esc(r.label) + '</div><div class="s">' + esc(r.sub) + "</div></div>";
  }
  data.models.forEach((m, mi) => {
    const vendor = m.includes("/") ? m.split("/")[0] : "";
    cells += '<div class="row-label"><b>' + esc(short(m)) + "</b><br><span>" + esc(vendor) + "</span></div>";
    for (const r of data.rounds) {
      const stage = data.stages[r.key];
      if (!stage) { cells += '<div class="placeholder">not run</div>'; continue; }
      const e = stage[mi];
      if (e.ok) {
        cells += '<button class="node" data-m="' + mi + '" data-r="' + esc(r.key) + '" style="--vc:' + vcolor(e) + '">' +
          '<span class="v"><i></i>' + esc(e.verdict) + '</span><span class="c">' + esc(e.confidence) + " confidence</span></button>";
      } else {
        cells += '<button class="node fail" data-m="' + mi + '" data-r="' + esc(r.key) + '">' +
          '<span class="v">✕ failed</span><span class="c">' + esc(e.status) + "</span></button>";
      }
    }
  });
  cells += '<div class="agg-label">Committee ballot</div>';
  for (const r of data.rounds) {
    const agg = data.aggregations[r.key];
    if (!agg) { cells += '<div class="placeholder">—</div>'; continue; }
    if (agg.aggregation_status !== "ok") {
      cells += '<div class="agg" style="color:var(--muted)">failed</div>';
    } else {
      const votes = data.stages[r.key].filter(e => e.ok).map(e => e.verdict);
      const tally = ["YES", "NO", "ABSTAIN"].map(v => v + " " + votes.filter(x => x === v).length).join(" / ");
      cells += '<div class="agg" style="color:' +
        ballotColor(agg.verdict) + '">' + esc(agg.verdict) +
        ' <span class="tally">' + tally + "</span></div>";
    }
  }
  grid.innerHTML = cells;

  /* connectors */
  const svg = document.getElementById("edges");
  function drawEdges() {
    const flow = document.getElementById("flow");
    const fr = flow.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + fr.width + " " + fr.height);
    let lines = "";
    const keys = data.rounds.map(r => r.key).filter(k => data.stages[k]);
    data.models.forEach((m, mi) => {
      for (let i = 0; i + 1 < keys.length; i++) {
        const a = grid.querySelector('[data-m="' + mi + '"][data-r="' + keys[i] + '"]');
        const b = grid.querySelector('[data-m="' + mi + '"][data-r="' + keys[i + 1] + '"]');
        if (!a || !b) continue;
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        const ea = data.stages[keys[i]][mi], eb = data.stages[keys[i + 1]][mi];
        const flipped = ea.ok && eb.ok && ea.verdict !== eb.verdict;
        const color = flipped
          ? ballotColor(eb.verdict)
          : "var(--grid)";
        lines += '<line x1="' + (ra.right - fr.left) + '" y1="' + (ra.top + ra.height / 2 - fr.top) +
          '" x2="' + (rb.left - fr.left) + '" y2="' + (rb.top + rb.height / 2 - fr.top) +
          '" stroke="' + color + '" stroke-width="' + (flipped ? 2.5 : 1.5) + '"' +
          (flipped ? "" : ' stroke-dasharray="none"') + " />";
        if (flipped) {
          const mx = (ra.right + rb.left) / 2 - fr.left, my = (ra.top + ra.height / 2 + rb.top + rb.height / 2) / 2 - fr.top;
          lines += '<circle cx="' + mx + '" cy="' + my + '" r="3.5" fill="' + color + '" />';
        }
      }
    });
    svg.innerHTML = lines;
  }
  new ResizeObserver(drawEdges).observe(document.getElementById("flow"));
  drawEdges();

  /* tooltip */
  const tip = document.getElementById("tooltip");
  grid.addEventListener("mousemove", ev => {
    const n = ev.target.closest(".node");
    if (!n) { tip.style.display = "none"; return; }
    const e = data.stages[n.dataset.r][+n.dataset.m];
    let html = "<b>" + esc(short(e.model)) + " · " + esc(n.dataset.r) + "</b><br>";
    if (e.ok) {
      const prev = prevEntry(+n.dataset.m, n.dataset.r);
      if (prev && prev.ok && prev.verdict !== e.verdict)
        html += "changed " + esc(prev.verdict) + " → <b>" + esc(e.verdict) + "</b><br>";
      if (e.decisive_provision) html += "decisive: " + esc(e.decisive_provision) + "<br>";
      html += esc(e.rationale.split(/\s+/).slice(0, 24).join(" ")) + "…";
    } else {
      html += esc(e.status) + (e.error ? " — " + esc(String(e.error).slice(0, 120)) : "");
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - w - 8) + "px";
    tip.style.top = (ev.clientY + 16 + h > innerHeight ? ev.clientY - h - 10 : ev.clientY + 16) + "px";
  });
  grid.addEventListener("mouseleave", () => { tip.style.display = "none"; });

  function prevEntry(mi, rk) {
    const keys = data.rounds.map(r => r.key);
    const i = keys.indexOf(rk);
    if (i <= 0) return null;
    const st = data.stages[keys[i - 1]];
    return st ? st[mi] : null;
  }

  /* detail panel */
  const detail = document.getElementById("detail");
  grid.addEventListener("click", ev => {
    const n = ev.target.closest(".node");
    if (!n) return;
    grid.querySelectorAll(".node.sel").forEach(x => x.classList.remove("sel"));
    n.classList.add("sel");
    const e = data.stages[n.dataset.r][+n.dataset.m];
    const round = data.rounds.find(r => r.key === n.dataset.r);
    let html = "<h2>" + esc(e.model) + "</h2>" +
      '<div class="sub">' + esc(round.label) + " · " + esc(round.sub) + "</div>";
    if (e.ok) {
      html += '<span class="chip" style="--vc:' + vcolor(e) + '"><i></i>' + esc(e.verdict) +
        " · " + esc(e.confidence) + " confidence</span>";
      const prev = prevEntry(+n.dataset.m, n.dataset.r);
      if (prev && prev.ok && prev.verdict !== e.verdict)
        html += '<div class="prose" style="margin-top:8px">Changed position: voted ' +
          esc(prev.verdict) + " in the previous round.</div>";
      html += '<div class="kv">Rationale</div><div class="prose">' + esc(e.rationale) + "</div>";
      if (e.strongest_counterargument) html += '<div class="kv">Strongest counterargument</div><div class="prose">' + esc(e.strongest_counterargument) + "</div>";
      if (e.revision_reason) html += '<div class="kv">Why this position changed or stayed</div><div class="prose">' + esc(e.revision_reason) + "</div>";
      for (const response of e.argument_responses) {
        const argument = data.review_arguments[response.argument_id];
        html += '<div class="prov"><b>' + esc(response.argument_id) + " · " + esc(response.assessment) + "</b>" +
          '<div class="meta">' + esc(argument ? argument.provision + ": " + argument.reasoning : "") + "</div>" +
          '<div class="prose">' + esc(response.reasoning) + '</div><div class="meta">Evidence: ' + esc(response.evidence) + "</div></div>";
      }
      for (const item of e.missing_information) {
        html += '<div class="prov"><b>Missing information · ' + esc(item.provision) + "</b>" +
          '<div class="prose">' + esc(item.question) + "</div>" +
          '<div class="prose">Why decisive: ' + esc(item.why_decisive) + "</div>" +
          '<div class="prose">If true: ' + esc(item.if_true) + "</div>" +
          '<div class="prose">If false: ' + esc(item.if_false) + "</div>" +
          '<div class="meta">Needed in next retrieval: ' + esc(item.source) + "</div></div>";
      }
      html += '<div class="kv">Decisive provision</div><div class="prose mono">' +
        (e.decisive_provision ? esc(e.decisive_provision) : "<i>none</i>") + "</div>";
      if (e.engaged_provisions.length) {
        html += '<div class="kv">Engaged provisions (' + e.engaged_provisions.length + ")</div>";
        for (const p of e.engaged_provisions) {
          const mark = p.assessment === "satisfied" ? "✓" : p.assessment === "violated" ? "✕" : p.assessment === "cannot_determine" ? "?" : "–";
          html += '<div class="prov"><div class="p-head"><span class="mono">' + esc(p.provision) +
            '</span><span class="assess ' + esc(p.assessment) + '">' + mark + " " +
            esc(p.assessment.replace("_", " ")) + "</span></div>" +
            '<div class="req">' + esc(p.requirement) + "</div>" +
            '<div class="rea">' + esc(p.reasoning) + '</div><div class="meta">Evidence: ' + esc(p.evidence || "") + "</div></div>";
        }
      }
    } else {
      html += '<span class="chip" style="--vc:var(--muted)"><i></i>' + esc(e.status) + "</span>";
      if (e.error) html += '<div class="kv">Error</div><div class="prose">' + esc(e.error) + "</div>";
      if (e.raw) html += '<div class="kv">Raw output (truncated record)</div><pre class="raw">' + esc(e.raw) + "</pre>";
    }
    detail.innerHTML = html;
    if (matchMedia("(min-width: 1000px)").matches) detail.scrollTop = 0;
  });

  /* table view */
  const tbl = document.getElementById("tbl");
  let t = "<tr><th>Seat</th>";
  for (const r of data.rounds) t += "<th>" + esc(r.label) + " · " + esc(r.sub) + "</th>";
  t += "<th>Decisive provision (R2)</th></tr>";
  data.models.forEach((m, mi) => {
    t += '<tr><td class="seat">' + esc(m) + "</td>";
    for (const r of data.rounds) {
      const st = data.stages[r.key];
      const e = st ? st[mi] : null;
      let cell;
      if (!e) cell = '<span class="meta">not run</span>';
      else if (e.ok) cell = '<span class="badge" style="--vc:' + vcolor(e) + '"><i></i>' +
        esc(e.verdict) + ' <span class="bc">' + esc(e.confidence) + "</span></span>";
      else cell = '<span class="badge fail"><i></i>failed <span class="bc">' + esc(e.status) + "</span></span>";
      t += "<td>" + cell + "</td>";
    }
    const r2 = data.stages["R2"] ? data.stages["R2"][mi] : null;
    t += '<td class="mono">' + (r2 && r2.ok ? esc(r2.decisive_provision || "—") : "—") + "</td></tr>";
  });
  tbl.innerHTML = t;
})();
</script>
</body>
</html>
"""
