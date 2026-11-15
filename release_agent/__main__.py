import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import truststore

from .agent import AgentFailure, ReleaseEvidenceAgent
from .config import Settings, load_settings
from .importer import Importer, build_issue_fields, load_dataset
from .jira_client import JiraClient, JiraError
from .llm import LLMError, LLMNotConfigured, ToolSpec, create_llm
from .models import RunResult
from .report import overall_decision, write_outputs
from .tools import JiraEvidenceTools
from .trace import Trace

JIRA_SETTINGS = ("jira_base_url", "jira_email", "jira_api_token", "jira_project_key")
DEFAULT_PROBE_MODELS = [
    "minimaxai/minimax-m3.1-flash",
    "nvidia/nemotron-3-super-120b-a12b",
    "mistralai/mistral-large-4-1t-a52b",
    "z-ai/glm-5.3-flash",
    "deepseek-ai/deepseek-v4.1-flash",
]


def _jira(settings: Settings) -> JiraClient:
    return JiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)


def _llm(settings: Settings, model: str, **options):
    return create_llm(settings.llm_provider, model, settings.llm_api_key, settings.llm_base_url,
                      settings.llm_temperature, settings.llm_max_tokens, **options)


def _probe(llm) -> dict:
    tool = ToolSpec("get_ticket", "Read a ticket by id.", {
        "type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]})
    system = "You answer questions by calling tools. Never guess ticket contents."
    messages = [{"role": "user", "content": "What is the status of ticket REQ-07? Use the tool."}]
    started = time.perf_counter()
    first = llm.complete(system, messages, [tool])
    call = first.tool_calls[0] if first.tool_calls else None
    if not call or call.name != "get_ticket" or call.error or call.arguments.get("ticket_id") != "REQ-07":
        return {"ok": False, "reason": f"no valid tool call (text: {first.text[:120]!r})"}
    messages += [
        {"role": "assistant", "text": first.text, "tool_calls": first.tool_calls},
        {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": "Blocked by DEF-9"})},
    ]
    second = llm.complete(system, messages, [tool])
    ok = "DEF-9" in second.text or "blocked" in second.text.lower()
    tokens = sum(r.usage.input_tokens or 0 for r in (first, second)), sum(r.usage.output_tokens or 0 for r in (first, second))
    return {"ok": ok, "reason": "" if ok else f"tool result not used (text: {second.text[:120]!r})",
            "seconds": round(time.perf_counter() - started, 1), "tokens": tokens}


def cmd_llm_check(settings: Settings, args) -> int:
    models = args.models or DEFAULT_PROBE_MODELS
    passed = []
    for model in models:
        try:
            llm = _llm(settings, model, timeout=60.0, max_retries=1, max_poll_seconds=60.0)
            result = _probe(llm)
        except (LLMNotConfigured, LLMError) as exc:
            result = {"ok": False, "reason": str(exc)[:300]}
        print(f"{'PASS' if result['ok'] else 'FAIL'}  {model}  {result.get('seconds', '-')}s  tokens={result.get('tokens', '-')}  {result['reason']}")
        if result["ok"]:
            passed.append(model)
    print(f"Recommended LLM_MODEL: {passed[0]}" if passed else "No model passed the tool-calling probe.")
    return 0 if passed else 1


def cmd_check(settings: Settings, args) -> int:
    settings.require("jira_base_url", "jira_email", "jira_api_token")
    with _jira(settings) as jira:
        me = jira.myself()
        print(f"Authenticated as {me.get('displayName')} ({me.get('accountId')})")
        if not settings.jira_project_key:
            print("Accessible projects:")
            for project in jira.list_projects():
                print(f"  {project['key']}: {project['name']}")
            return 0
        try:
            project = jira.get_project(settings.jira_project_key)
        except JiraError as exc:
            print(f"Project {settings.jira_project_key} not accessible: {exc}")
            for p in jira.list_projects():
                print(f"  {p['key']}: {p['name']}")
            return 1
        print(f"Project {project['key']}: {project['name']} ({project.get('projectTypeKey')}, {project.get('style')})")
        print("Issue types:", ", ".join(t["name"] for t in project.get("issueTypes", [])))
        print("Link types:", ", ".join(t["name"] for t in jira.link_types()))
        jql = f'project = "{settings.jira_project_key}" AND labels = "{settings.dataset_label}"'
        print(f"Issues labelled {settings.dataset_label}: {sum(1 for _ in jira.search(jql, ['labels']))}")
    return 0


def cmd_import(settings: Settings, args) -> int:
    data = load_dataset(args.dataset)
    records = data["records"]
    print(f"Dataset {data['dataset_label']}: {len(records)} records, {sum(len(r['comments']) for r in records)} comments, "
          f"{sum(len(r['links']) for r in records)} links")
    if settings.dataset_label != data["dataset_label"]:
        print(f"Note: importer labels issues with the dataset's own label '{data['dataset_label']}'")
    if args.dry_run:
        sample = build_issue_fields(records[0], settings.jira_project_key or "PROJ", "<issue-type-id>", data["dataset_label"])
        print(json.dumps(sample, indent=2)[:2000])
        return 0
    settings.require(*JIRA_SETTINGS)
    started = time.perf_counter()
    with _jira(settings) as jira:
        summary = Importer(jira, settings.jira_project_key, settings.jira_issue_type, settings.jira_link_type).run(data)
        print(json.dumps(asdict(summary), indent=2))
        print(f"Import finished in {time.perf_counter() - started:.1f} s using {jira.request_count} Jira requests")
    return 0


def cmd_assess(settings: Settings, args) -> int:
    settings.require(*JIRA_SETTINGS)
    try:
        llm = _llm(settings, settings.llm_model)
    except LLMNotConfigured as exc:
        print(exc, file=sys.stderr)
        return 3
    run_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = Trace(run_dir / "trace.jsonl")
    started = time.perf_counter()
    with _jira(settings) as jira:
        tools = JiraEvidenceTools(jira, settings.jira_project_key, settings.dataset_label)
        agent = ReleaseEvidenceAgent(
            llm,
            tools,
            trace,
            target_release=settings.target_release,
            max_discovery_steps=settings.max_discovery_steps,
            max_requirement_steps=settings.max_requirement_steps,
            max_tool_calls=settings.max_tool_calls,
            max_runtime_seconds=settings.max_runtime_seconds,
            tool_result_max_chars=settings.tool_result_max_chars,
            max_parallel=settings.max_parallel,
        )
        trace.event("run_start", project=settings.jira_project_key, label=settings.dataset_label,
                    target_release=settings.target_release, provider=settings.llm_provider, model=settings.llm_model)
        discovery, decisions = None, []
        try:
            discovery, decisions = agent.run()
        except AgentFailure:
            pass
        result = RunResult(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            project_key=settings.jira_project_key,
            dataset_label=settings.dataset_label,
            target_release=settings.target_release,
            jira_base_url=settings.jira_base_url,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            overall_decision=overall_decision(decisions) if discovery else "NOT_ASSESSED",
            duration_s=round(time.perf_counter() - started, 2),
            usage=agent.usage,
            jira_requests=jira.request_count,
            discovery=discovery,
            decisions=decisions,
            errors=agent.errors,
        )
    paths = write_outputs(result, run_dir)
    trace.event("run_end", overall=result.overall_decision, duration_s=result.duration_s, errors=result.errors)
    trace.close()
    print(f"Overall decision: {result.overall_decision}")
    for path in [*paths.values(), trace.path]:
        print(f"  {path}")
    return 0 if discovery else 2


def main(argv: list[str] | None = None) -> int:
    # Verify TLS against the OS certificate store so corporate proxies with private CAs work.
    truststore.inject_into_ssl()
    parser = argparse.ArgumentParser(prog="release_agent", description="Jira release evidence agent")
    parser.add_argument("--project", dest="jira_project_key", help="Jira project key")
    parser.add_argument("--label", dest="dataset_label", help="dataset scope label")
    parser.add_argument("--release", dest="target_release", help="target release, e.g. R2.4")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify Jira access and project configuration")
    p_import = sub.add_parser("import", help="import the dataset into Jira (resumable)")
    p_import.add_argument("--dataset", default="input/dataset.json")
    p_import.add_argument("--dry-run", action="store_true", help="validate and preview without calling Jira")
    p_assess = sub.add_parser("assess", help="run the agent and write the report")
    p_assess.add_argument("--out", default="runs")
    p_llm = sub.add_parser("llm-check", help="probe models for tool calling")
    p_llm.add_argument("--models", nargs="+", help="model ids to probe (default: candidates)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(**vars(args))
    commands = {"check": cmd_check, "import": cmd_import, "assess": cmd_assess, "llm-check": cmd_llm_check}
    try:
        return commands[args.command](settings, args)
    except JiraError as exc:
        print(f"Jira error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
