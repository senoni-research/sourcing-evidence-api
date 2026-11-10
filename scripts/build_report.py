"""Render the recorded API responses as a portable, static HTML report."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from html import escape
from pathlib import Path


def text(value: object) -> str:
    return escape(str(value), quote=True)


def number(value: str, signed: bool = False) -> str:
    result = format(Decimal(value).normalize(), "f")
    return ("+" if signed and Decimal(value) > 0 else "") + result


def money(value: str) -> str:
    return f"£{Decimal(value):,.2f}"


def excerpts(response: dict, prefixes: tuple[str, ...]) -> str:
    selected = [item for item in response["evidence"] if item["quote"].startswith(prefixes)]
    return "".join(
        '<div class="excerpt"><div class="source">'
        f"{text(item['document'])} · page {item['page']}</div>"
        f"<code>{text(item['quote'])}</code></div>"
        for item in selected
    )


def build(source: Path, destination: Path) -> None:
    runs = json.loads(source.read_text(encoding="utf-8"))
    indexed = {item["case"]: item for item in runs}
    complete = indexed["price_increase"]["response"]
    calc = complete["calculation"]
    if calc["currency"] != "GBP":
        raise ValueError("This report's currency presentation supports GBP only.")

    contribution_rows = "".join(
        '<tr><th scope="row">'
        f"{text(item['name'].capitalize())}</th>"
        f"<td>{number(str(Decimal(item['share']) * 100))}%</td>"
        f"<td>{number(item['index_change_pct'], True)}%</td>"
        '<td class="effect"><span class="bar-track" aria-hidden="true">'
        f'<span class="bar {"negative" if Decimal(item["contribution_pp"]) < 0 else ""}" '
        f'style="width:{min(abs(Decimal(item["contribution_pp"])) / Decimal(3) * 100, 100)}%"></span>'
        f"</span><span>{number(item['contribution_pp'], True)}</span></td></tr>"
        for item in calc["contributions"]
    )

    withheld = []
    for index, key, title, prefixes in [
        ("02", "missing_evidence", "A missing index is not zero.", ("driver.energy",)),
        (
            "03",
            "scope_mismatch",
            "A new specification changes the question.",
            ("baseline.revision", "proposal.revision"),
        ),
    ]:
        run = indexed[key]
        response = run["response"]
        if response["calculation"] is not None:
            raise ValueError(f"Expected a withheld calculation for {key}.")
        issues = "".join(f"<li>{text(item['message'])}</li>" for item in response["issues"])
        withheld.append(
            '<article class="panel withheld">'
            f'<div class="case-top"><span class="eyebrow">CASE {index}</span>'
            f'<span class="status amber">{text(response["status"].replace("_", " "))}</span></div>'
            f"<h3>{title}</h3><p>{text(response['summary'])}</p>"
            '<div class="withheld-value">Calculation withheld <span>—</span></div>'
            f'<ul class="issue-list">{issues}</ul>'
            f"{excerpts(response, prefixes)}"
            f'<p class="meta">HTTP {run["http_status"]} · {len(response["evidence"])} source excerpts · '
            f"<code>{text(response['review_id'])}</code></p></article>"
        )

    assumptions = "".join(f"<li>{text(item)}</li>" for item in complete["assumptions"])
    questions = "".join(f"<li>{text(item)}</li>" for item in complete["questions"])
    limitations = "".join(f"<li>{text(item)}</li>" for item in complete["limitations"])
    recorded_date = next(
        item["quote"].split("=", 1)[1].strip()
        for item in complete["evidence"]
        if item["quote"].startswith("as_of =")
    )
    source_hash = complete["evidence"][0]["sha256"]
    body = TEMPLATE.replace("{{CONTRIBUTIONS}}", contribution_rows)
    replacements = {
        "{{BASELINE}}": money(calc["baseline_price"]),
        "{{PROPOSED}}": money(calc["proposed_price"]),
        "{{SCENARIO}}": money(calc["scenario_price"]),
        "{{PROPOSED_CHANGE}}": number(calc["proposed_change_pct"], True),
        "{{SCENARIO_CHANGE}}": number(calc["model_change_pct"], True),
        "{{UNIT}}": text(calc["unit"]),
        "{{GAP}}": money(calc["gap_per_unit"]),
        "{{ANNUAL_GAP}}": money(calc["annualised_gap"]),
        "{{STATUS}}": text(complete["status"].replace("_", " ")),
        "{{SUMMARY}}": text(complete["summary"]),
        "{{HTTP_STATUS}}": str(indexed["price_increase"]["http_status"]),
        "{{SOURCE_COUNT}}": str(len(complete["evidence"])),
        "{{REVIEW_ID}}": text(complete["review_id"]),
        "{{EVIDENCE}}": excerpts(
            complete,
            ("baseline.unit_price", "proposal.unit_price", "driver.energy", "annual_volume"),
        ),
        "{{WITHHELD}}": "".join(withheld),
        "{{ASSUMPTIONS}}": assumptions,
        "{{QUESTIONS}}": questions,
        "{{LIMITATIONS}}": limitations,
        "{{MODEL_VERSION}}": text(complete["model_version"]),
        "{{AS_OF}}": text(recorded_date),
        "{{SOURCE_HASH}}": text(source_hash),
        "{{FINGERPRINT}}": text(complete["analysis_fingerprint"]),
        "{{RAW_JSON}}": text(json.dumps(runs, indent=2, ensure_ascii=False)),
    }
    for key, value in replacements.items():
        body = body.replace(key, value)
    if "{{" in body:
        raise ValueError("An unresolved template variable remains.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body, encoding="utf-8")
    print(destination)


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="A recorded demonstration of a sourcing evidence API: a conditional price scenario, missing evidence and changed specification.">
<title>SENONI · Sourcing evidence — Recorded API demonstration</title>
<style>
:root{--paper:#f6f4ec;--white:#fffefa;--ink:#132e3d;--muted:#627782;--teal:#0e716f;--soft:#e8f2ed;--line:#d7ddd3;--amber:#78531b;--amber-bg:#f6eedc}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main,header,footer{max-width:1180px;margin:auto;padding:0 36px}header{display:flex;justify-content:space-between;align-items:center;gap:20px;padding-top:25px;padding-bottom:25px;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:13px;font-size:18px;font-weight:750;letter-spacing:3px}.mark{border:1px solid var(--ink);border-radius:50%;width:32px;height:32px;display:grid;place-items:center;font:20px Georgia,serif;letter-spacing:0}.subtitle{font-size:13px;color:var(--muted);letter-spacing:0;font-weight:400;border-left:1px solid var(--line);padding-left:13px}.small{font-size:12px;color:var(--muted)}.hero{padding:44px 0 32px}.eyebrow{font-size:11px;font-weight:800;letter-spacing:1.9px;color:var(--teal);text-transform:uppercase}h1,h2,h3{font-family:Georgia,"Times New Roman",serif;font-weight:400;line-height:1.15;margin:12px 0 16px}h1{font-size:48px;max-width:900px;letter-spacing:-1px}h2{font-size:34px}h3{font-size:27px}p{margin:8px 0 16px;color:var(--muted)}.intro{max-width:760px;font-size:17px}.recorded{display:flex;align-items:center;gap:10px;width:fit-content;background:var(--ink);color:white;padding:10px 16px;border-radius:5px;font-size:13px;font-weight:650;margin:22px 0 0}.dot{height:7px;width:7px;border-radius:50%;background:#b4cfbc;flex:0 0 7px}.recorded-note{font-size:12px;margin-top:9px}.panel{background:var(--white);border:1px solid var(--line);border-radius:16px;padding:28px;min-width:0}.case-top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.status{display:inline-block;background:var(--soft);color:var(--teal);text-transform:uppercase;font-weight:750;letter-spacing:.6px;font-size:10px;border-radius:4px;padding:4px 8px}.status.amber{background:var(--amber-bg);color:var(--amber)}.primary{margin-bottom:20px}.result-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--line);border-radius:9px;overflow:hidden;margin:25px 0 14px}.metric{min-width:0;padding:20px;border-right:1px solid var(--line)}.metric:last-child{border-right:0;background:var(--soft)}.metric-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}.metric-value{font-family:Georgia,serif;font-size:36px;line-height:1.3;margin:6px 0}.metric:last-child .metric-value{color:var(--teal)}.metric-note{font-size:12px;color:var(--muted)}.gap{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:0 0 22px;border-bottom:1px solid var(--line);font-size:13px}.gap strong{font-size:20px}.gap .annual{text-align:right}.gap small{display:block;color:var(--muted);font-size:11px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}.primary-columns{margin-top:24px;grid-template-columns:1.05fr 1fr;gap:30px}.section-label{font-size:14px;font-weight:750;margin:0 0 14px}table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}th,td{padding:12px 0;border-bottom:1px solid #e7ebe4;text-align:right}thead th{font-size:10px;color:var(--muted);font-weight:500}th:first-child{text-align:left;width:27%}th:nth-child(2){width:17%}th:nth-child(3){width:22%}th:nth-child(4){width:34%}tbody th{font-weight:650}.effect{display:flex;align-items:center;justify-content:flex-end;gap:12px}.effect>span:last-child{width:27px;flex:0 0 27px}.bar-track{display:block;width:76px;max-width:50%;height:6px;background:#e8ece5;border-radius:4px;overflow:hidden}.bar{display:block;height:6px;border-radius:4px;background:#80978a}.bar.negative{background:#42a08d}.formula{font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:#f0f2ec;border-radius:6px;padding:12px;margin:16px 0 0;overflow-wrap:anywhere;color:var(--muted)}.excerpt{padding:11px 13px;background:#f8f9f4;border:1px solid #e0e4db;border-radius:7px;margin-bottom:9px;min-width:0}.source{font-size:10px;color:var(--teal);font-weight:700;margin-bottom:5px}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.excerpt code{font-size:11px;line-height:1.5;display:block;color:var(--ink)}.meta{font-size:10px;margin:18px 0 0;color:var(--muted);overflow-wrap:anywhere}.callout{background:#f0f3ed;border-top:1px solid var(--line);margin:25px -28px -28px;padding:16px 28px;border-radius:0 0 16px 16px;font-size:12px;color:var(--muted)}.withheld h3{min-height:63px}.withheld-value{padding:13px 0;border-block:1px solid var(--line);font-weight:650;display:flex;justify-content:space-between;margin:18px 0 12px}.withheld-value span{color:var(--amber);font-size:20px}ul,ol{padding-left:19px;color:var(--muted);font-size:13px}li{padding-left:2px;margin:8px 0}.issue-list{min-height:44px}.section{padding-top:38px}.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:20px}.section-head p{font-size:12px}.architecture{display:grid;grid-template-columns:repeat(4,1fr);gap:0;background:var(--ink);color:white;border-radius:14px;overflow:hidden;margin:16px 0 20px}.stage{padding:25px 23px;border-right:1px solid #37505c}.stage:last-child{border:0}.stage-number{font:12px ui-monospace,monospace;color:#a4c7b7}.stage h4{font-size:15px;line-height:1.4;margin:13px 0 9px}.stage p{font-size:12px;color:#bdcbd0;margin:0}.boundary{border-left:3px solid var(--teal);padding:3px 0 3px 18px;font-size:13px;margin:20px 0}.boundary strong{color:var(--ink)}.boundary p{margin:5px 0}.plain h3{font-family:inherit;font-size:16px;font-weight:700;margin-top:0}.plain{padding:23px 25px}.technical{margin-top:20px}.technical summary{cursor:pointer;font-size:12px;font-weight:700;padding:13px 0;border-block:1px solid var(--line)}.technical-content{padding:15px 0;font-size:11px;color:var(--muted)}.technical-content p{margin:7px 0}.technical-content pre{font-size:10px;white-space:pre-wrap;overflow-wrap:anywhere;background:#eef0e9;border-radius:6px;padding:16px;max-height:380px;overflow:auto}.technical-content code{display:block}footer{display:flex;justify-content:space-between;gap:20px;border-top:1px solid var(--line);margin-top:38px;padding-top:20px;padding-bottom:30px;font-size:11px;color:var(--muted)}.footer-brand{font-weight:750}a{color:var(--teal)}
@media(max-width:760px){main,header,footer{padding-left:20px;padding-right:20px}header{padding-top:20px;padding-bottom:20px}.subtitle{display:none}header>.small{max-width:130px;text-align:right;font-size:10px}.hero{padding-top:30px}h1{font-size:37px}h2{font-size:29px}.intro{font-size:15px}.panel{padding:20px}.two-col,.primary-columns{grid-template-columns:1fr}.result-grid{margin-top:20px}.metric{padding:12px 9px}.metric-value{font-size:21px}.metric-label{font-size:8px;letter-spacing:.5px}.metric-note{font-size:10px}.gap{gap:12px}.gap strong{font-size:17px}.gap small{font-size:10px}.gap>div{max-width:52%}.callout{margin:20px -20px -20px;padding:15px 20px}.withheld h3,.issue-list{min-height:0}.section{padding-top:28px}.section-head{display:block}.section-head p{margin-top:-7px}.architecture{grid-template-columns:1fr 1fr}.stage{padding:20px 17px}.stage:nth-child(2){border-right:0}.stage:nth-child(-n+2){border-bottom:1px solid #37505c}.stage h4{font-size:14px}.stage p{font-size:11px}.recorded{font-size:12px;padding:10px 12px}footer{flex-direction:column;gap:6px}.primary-columns{gap:24px}.effect{gap:7px}}
@media print{body{font-size:11px;background:white}header,main,footer{max-width:none;padding-left:0;padding-right:0}.hero{padding-top:20px}h1{font-size:34px}.panel{break-inside:avoid}.section{break-inside:avoid}.technical{display:none}.architecture{print-color-adjust:exact}.recorded,.metric:last-child,.status{print-color-adjust:exact}}
</style></head><body>
<header><div class="brand"><span class="mark">S</span>SENONI<span class="subtitle">Sourcing evidence</span></div><div class="small">Independent engineering prototype<br>Fictional supplier data</div></header>
<main>
<section class="hero"><div class="eyebrow">From supplier evidence to a clearer conversation</div><h1>What explains the price change?</h1><p class="intro">A supplier requests an increase. This API makes the declared cost drivers inspectable, shows a conditional price scenario, and withholds the calculation when the evidence does not support a comparison.</p><div class="recorded"><span class="dot"></span>Recorded API run — not a live service</div><p class="recorded-note">Three actual responses from the packaged document examples. Scenario as-of date: {{AS_OF}}.</p></section>
<section class="panel primary"><div class="case-top"><span class="eyebrow">Case 01 · Complete evidence</span><span class="status">{{STATUS}}</span></div><h2>A price change you can inspect.</h2><p>{{SUMMARY}}</p>
<div class="result-grid"><div class="metric"><div class="metric-label">Baseline price</div><div class="metric-value">{{BASELINE}}</div><div class="metric-note">per {{UNIT}} · GBP</div></div><div class="metric"><div class="metric-label">Supplier proposal</div><div class="metric-value">{{PROPOSED}}</div><div class="metric-note">{{PROPOSED_CHANGE}}% from baseline</div></div><div class="metric"><div class="metric-label">Indexed scenario</div><div class="metric-value">{{SCENARIO}}</div><div class="metric-note">{{SCENARIO_CHANGE}}% from declared drivers</div></div></div>
<div class="gap"><div><strong>{{GAP}}</strong> per {{UNIT}}<small>Proposal minus indexed scenario</small></div><div class="annual"><strong>{{ANNUAL_GAP}}</strong><small>Annualised scenario difference · not savings</small></div></div>
<div class="two-col primary-columns"><div><h4 class="section-label">What drives the scenario</h4><table><thead><tr><th scope="col">Cost driver</th><th scope="col">Share</th><th scope="col">Index change</th><th scope="col">Effect, pp</th></tr></thead><tbody>{{CONTRIBUTIONS}}</tbody></table><div class="formula">Scenario = baseline × (1 + Σ share × index change)</div><p class="small">Effects are percentage-point contributions to the baseline price. The declared basket is applied to the entire price.</p><h4 class="section-label" style="margin-top:25px">Questions for the supplier</h4><ol>{{QUESTIONS}}</ol></div><div><h4 class="section-label">Selected source excerpts</h4><p class="small">Quotations matched to uploaded source text. References establish traceability, not commercial validity.</p>{{EVIDENCE}}<p class="meta">HTTP {{HTTP_STATUS}} · {{SOURCE_COUNT}} source excerpts<br><code>{{REVIEW_ID}}</code></p></div></div>
<div class="callout">This is a conditional cost-driver scenario, not a verified supplier cost. The difference is a discussion point; margin, hedging, productivity and contract terms may also matter.</div></section>
<div class="two-col">{{WITHHELD}}</div>
<section class="section"><div class="section-head"><div><div class="eyebrow">Engineering choices</div><h2>Evidence first. Arithmetic in code.</h2></div><p>FastAPI · typed validation · decimal arithmetic</p></div>
<div class="architecture"><div class="stage"><span class="stage-number">01 / INPUT</span><h4>Document or JSON</h4><p>Small PDF/text uploads or structured facts enter the API. Input limits constrain the demonstration.</p></div><div class="stage"><span class="stage-number">02 / EXTRACT</span><h4>Typed facts + sources</h4><p>The labelled-template parser supplies facts, page references and quotations. Source identity and text are checked.</p></div><div class="stage"><span class="stage-number">03 / CHECK</span><h4>Comparability gates</h4><p>Schema, quote scope and evidence completeness determine whether a calculation is permitted.</p></div><div class="stage"><span class="stage-number">04 / REVIEW</span><h4>Deterministic scenario</h4><p>Decimal arithmetic returns contributions, explicit status, questions and an analysis fingerprint.</p></div></div>
<div class="boundary"><strong>Labelled-template extraction is the default. No model call was used in these recorded cases.</strong><p>An optional OpenAI-compatible adapter can extract typed facts; it does not do the arithmetic or execute tools. Provider behaviour was tested with mocked responses. Live model extraction accuracy has not been validated.</p></div>
</section>
<section class="section"><div class="eyebrow">Interpretation &amp; scope</div><h2>A useful result includes its boundaries.</h2><div class="two-col"><div class="panel plain"><h3>Assumptions behind the scenario</h3><ul>{{ASSUMPTIONS}}</ul><h3 style="margin-top:24px">What the result can establish</h3><ul>{{LIMITATIONS}}</ul></div><div class="panel plain"><h3>Prototype boundaries</h3><ul><li>All documents, supplier details and indices are fictional. There is no connection to any real buyer, procurement systems or live market data.</li><li>The default parser requires the published labelled format. There is no OCR. GBP is the only supported scenario currency.</li><li>The API can withhold a calculation; it does not approve prices, contact suppliers or create purchase orders.</li><li>Local operation is the demonstrated scope. Public uploads require isolated document processing, enterprise access controls and deployment review.</li><li>Requests are processed in memory. There is no durable audit store, tenant isolation or production operational history.</li></ul></div></div>
<details class="technical"><summary>Recorded response provenance and full JSON</summary><div class="technical-content"><p>Source: <code>examples/recorded_runs.json</code></p><p>Model version: <code>{{MODEL_VERSION}}</code></p><p>Complete-case source SHA-256: <code>{{SOURCE_HASH}}</code></p><p>Complete-case analysis fingerprint: <code>{{FINGERPRINT}}</code></p><p>The recorded bodies below are preserved as supplied. Request identifiers are specific to these recorded calls; the analysis fingerprint identifies the deterministic analysis.</p><pre>{{RAW_JSON}}</pre></div></details>
</section>
</main><footer><div><span class="footer-brand">SENONI</span> · Sourcing evidence API · Independent prototype</div><div>Developed with AI assistance. Human review remains essential.</div></footer></body></html>
"""


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "examples" / "recorded_runs.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.input, args.output)
