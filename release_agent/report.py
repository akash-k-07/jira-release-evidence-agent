import json
from dataclasses import asdict
from pathlib import Path

from .models import RequirementDecision, RunResult


def overall_decision(decisions: list[RequirementDecision]) -> str:
    mandatory = [d for d in decisions if d.requirement.mandatory]
    if any(d.decision == "FAIL" for d in mandatory):
        return "BLOCKED"
    if not mandatory or any(d.decision != "PASS" for d in mandatory):
        return "NEEDS_REVIEW"
    return "READY"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(result: RunResult) -> str:
    lines = [
        "# Release Readiness Report",
        "",
        f"**Overall decision: {result.overall_decision}**",
        "",
        f"- Generated: {result.generated_at}",
        f"- Jira project: {result.project_key} ({result.jira_base_url})",
        f"- Dataset scope label: `{result.dataset_label}`",
        f"- Target release: {result.target_release}",
    ]
    if result.discovery:
        d = result.discovery
        target = ", ".join(f"{k} {v}" for k, v in d.target.items())
        lines += [f"- Target build: {target}", f"- Release manifest: {d.manifest_source_id} ([{d.manifest_key}]({result.jira_base_url}/browse/{d.manifest_key}))"]
    lines += [f"- Model: {result.llm_provider} / {result.llm_model or 'n/a'}", ""]

    counts = {k: sum(1 for d in result.decisions if d.decision == k) for k in ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")}
    lines += [
        f"PASS: {counts['PASS']} | FAIL: {counts['FAIL']} | INSUFFICIENT_EVIDENCE: {counts['INSUFFICIENT_EVIDENCE']}",
        "",
        "## Summary",
        "",
        "| Requirement | Jira | Title | Decision | Status |",
        "|---|---|---|---|---|",
    ]
    for d in result.decisions:
        r = d.requirement
        lines.append(f"| {r.source_id} | {r.issue_key} | {_cell(r.summary)} | **{d.decision}** | {d.status} |")

    lines += ["", "## Requirement decisions", ""]
    for d in result.decisions:
        lines += _decision_section(d)

    if result.errors:
        lines += ["## Tool and model failures", ""] + [f"- {e}" for e in result.errors] + [""]

    u = result.usage
    tokens = f"{u.input_tokens} input / {u.output_tokens} output" if u.token_usage_reported else "not reported by provider"
    lines += [
        "## Execution",
        "",
        f"- Duration: {result.duration_s:.1f} s",
        f"- Model calls: {u.llm_calls} (errors: {u.llm_errors})",
        f"- Tokens: {tokens}",
        f"- Agent tool calls: {u.tool_calls} (errors: {u.tool_errors})",
        f"- Jira HTTP requests: {result.jira_requests}",
        "",
        "Native Jira authors and timestamps belong to the importer; actors, roles and times above come from the recorded comment text.",
        "",
    ]
    return "\n".join(lines)


def _decision_section(d: RequirementDecision) -> list[str]:
    r = d.requirement
    lines = [
        f"### {r.source_id}: {r.summary} - {d.decision}",
        "",
        f"- Jira issue: {r.issue_key}",
        f"- Acceptance criterion: {r.criterion}",
        f"- Mandatory: {'yes' if r.mandatory else 'no'}",
        "",
        d.explanation,
        "",
    ]
    if d.evidence:
        lines += ["**Evidence**", ""]
        for e in d.evidence:
            ref = f"[{e.issue_key}]({e.url})" + (f" comment {e.comment_id} (#{e.comment_index})" if e.comment_id else "")
            who = f" - {e.recorded_at}, {e.actor} ({e.actor_role})" if e.recorded_at else ""
            lines.append(f"- *{e.role}* {e.source_id} {ref}{who}: {e.note}")
            if e.excerpt:
                lines.append(f"  > {_cell(e.excerpt)}")
        lines.append("")
    if d.outstanding:
        lines += ["**Outstanding verification or clarification**", ""] + [f"- {x}" for x in d.outstanding] + [""]
    if d.concerns:
        lines += ["**Concerns**", ""] + [f"- {x}" for x in d.concerns] + [""]
    if d.error:
        lines += [f"**Agent error:** {d.error}", ""]
    return lines


def write_outputs(result: RunResult, out_dir: Path) -> dict[str, Path]:
    paths = {"report_md": out_dir / "report.md", "report_json": out_dir / "report.json", "metrics": out_dir / "metrics.json"}
    paths["report_md"].write_text(render_markdown(result), encoding="utf-8")
    paths["report_json"].write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    metrics = {
        "duration_s": result.duration_s,
        "overall_decision": result.overall_decision,
        "usage": asdict(result.usage),
        "jira_requests": result.jira_requests,
        "per_requirement": {
            d.requirement.source_id: {"decision": d.decision, "status": d.status, "duration_s": d.duration_s, **asdict(d.usage)}
            for d in result.decisions
        },
    }
    paths["metrics"].write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return paths
