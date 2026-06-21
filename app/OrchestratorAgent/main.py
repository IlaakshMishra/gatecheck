import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, TypedDict

import boto3
import requests
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langchain_aws import ChatBedrockConverse
from langgraph.graph import END, StateGraph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

AC_VERIFIER_ARN = os.environ["AC_VERIFIER_ARN"]
SECURITY_AUDITOR_ARN = os.environ["SECURITY_AUDITOR_ARN"]
PERF_ANALYZER_ARN = os.environ["PERF_ANALYZER_ARN"]
STYLE_ENFORCER_ARN = os.environ["STYLE_ENFORCER_ARN"]
GITHUB_SECRET_ARN = os.environ["GITHUB_SECRET_ARN"]
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

bedrock_agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

llm = ChatBedrockConverse(model_id=MODEL_ID)


class OrchestratorState(TypedDict):
    pr_diff: str
    acceptance_criteria: str
    pr_description: str
    repo_url: str
    pr_number: int
    ac_items: list[str]
    ac_bucket: list[str]
    security_bucket: list[str]
    perf_bucket: list[str]
    style_bucket: list[str]
    ac_report: dict
    security_report: dict
    perf_report: dict
    style_report: dict
    final_verdict: dict


def _ensure_session_id(session_id: str) -> str:
    min_len = 33
    if len(session_id) < min_len:
        pad = uuid.uuid4().hex
        session_id = session_id + pad
    return session_id[:128]


def _get_github_token() -> str:
    resp = secrets_client.get_secret_value(SecretId=GITHUB_SECRET_ARN)
    secret = resp.get("SecretString", "{}")
    try:
        data = json.loads(secret)
        return data.get("github_token", data.get("token", secret))
    except json.JSONDecodeError:
        return secret.strip()


def _parse_repo_info(repo_url: str) -> tuple[str, str]:
    match = re.search(r"github\.com[:/]([^/]+)/([^/\.]+)", repo_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub repo URL: {repo_url}")
    return match.group(1), match.group(2).rstrip(".git")


def _post_pr_comment(repo_url: str, pr_number: int, body: str) -> None:
    token = _get_github_token()
    owner, repo = _parse_repo_info(repo_url)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    resp.raise_for_status()
    logger.info("Posted comment to PR #%s, status=%s", pr_number, resp.status_code)


async def _invoke_sub_agent(agent_arn: str, payload: dict, agent_label: str) -> dict:
    session_id = _ensure_session_id(f"orch-{agent_label}-{uuid.uuid4().hex}")

    def _call():
        # AgentCore data-plane: "bedrock-agentcore", method invoke_agent_runtime
        return bedrock_agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            payload=json.dumps(payload).encode("utf-8"),
            runtimeSessionId=session_id,
        )

    try:
        resp = await asyncio.to_thread(_call)
        # Response body is a StreamingBody under the "response" key
        raw_bytes = resp["response"].read()
        text = raw_bytes.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            return {"raw_output": text, "error": "non-json response"}
    except Exception as exc:
        logger.error("Sub-agent %s failed: %s", agent_label, exc)
        return {"error": str(exc), "agent": agent_label, "findings": []}


def node_parse_ac(state: OrchestratorState) -> OrchestratorState:
    criteria_text = state["acceptance_criteria"]
    system = "You are a requirements parser. Return ONLY valid JSON, no prose."
    human = (
        f"Parse these acceptance criteria into a JSON array of strings. "
        f"Each string is one criterion.\n\n{criteria_text}"
    )
    response = llm.invoke([
        {"role": "system", "content": system},
        {"role": "user", "content": human},
    ])
    raw = response.content if hasattr(response, "content") else str(response)
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            items = [str(items)]
    except json.JSONDecodeError:
        array_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if array_match:
            items = json.loads(array_match.group(0))
        else:
            items = [line.strip("- ").strip() for line in criteria_text.splitlines() if line.strip()]
    state["ac_items"] = items
    return state


def node_categorize(state: OrchestratorState) -> OrchestratorState:
    items = state["ac_items"]
    system = "You are a requirements categorizer. Return ONLY valid JSON, no prose."
    human = (
        "Categorize each acceptance criterion into exactly one of: "
        "'ac', 'security', 'performance', 'style'. "
        "Return JSON object with keys 'ac', 'security', 'performance', 'style', "
        "each containing an array of criterion strings.\n\n"
        f"Criteria:\n{json.dumps(items)}"
    )
    response = llm.invoke([
        {"role": "system", "content": system},
        {"role": "user", "content": human},
    ])
    raw = response.content if hasattr(response, "content") else str(response)
    try:
        buckets = json.loads(raw)
    except json.JSONDecodeError:
        obj_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if obj_match:
            buckets = json.loads(obj_match.group(0))
        else:
            buckets = {"ac": items, "security": [], "performance": [], "style": []}

    state["ac_bucket"] = buckets.get("ac", [])
    state["security_bucket"] = buckets.get("security", [])
    state["perf_bucket"] = buckets.get("performance", [])
    state["style_bucket"] = buckets.get("style", [])
    return state


async def node_fan_out(state: OrchestratorState) -> OrchestratorState:
    pr_diff = state["pr_diff"]
    all_ac_items = state["ac_items"]

    ac_payload = {
        "pr_diff": pr_diff,
        "acceptance_criteria_list": state["ac_bucket"],
    }
    # Security always receives ALL AC items — runs unconditionally regardless of categorization
    security_payload = {
        "pr_diff": pr_diff,
        "ac_security_items": all_ac_items,
    }
    perf_payload = {
        "pr_diff": pr_diff,
        "ac_perf_items": state["perf_bucket"],
    }
    style_payload = {
        "pr_diff": pr_diff,
        "ac_style_items": state["style_bucket"],
    }

    results = await asyncio.gather(
        _invoke_sub_agent(AC_VERIFIER_ARN, ac_payload, "ac-verifier"),
        _invoke_sub_agent(SECURITY_AUDITOR_ARN, security_payload, "security-auditor"),
        _invoke_sub_agent(PERF_ANALYZER_ARN, perf_payload, "perf-analyzer"),
        _invoke_sub_agent(STYLE_ENFORCER_ARN, style_payload, "style-enforcer"),
        return_exceptions=False,
    )

    state["ac_report"] = results[0]
    state["security_report"] = results[1]
    state["perf_report"] = results[2]
    state["style_report"] = results[3]
    return state


def node_synthesize(state: OrchestratorState) -> OrchestratorState:
    ac_report = state["ac_report"]
    security_report = state["security_report"]
    perf_report = state["perf_report"]
    style_report = state["style_report"]

    all_findings: list[dict] = []
    overall_pass = True

    ac_criteria = ac_report.get("criteria", []) if isinstance(ac_report, dict) else []
    for item in ac_criteria:
        if isinstance(item, dict) and item.get("status") not in ("PASS", "UNVERIFIABLE"):
            overall_pass = False
        if isinstance(item, dict):
            all_findings.append({
                "category": "AC",
                "severity": "HIGH" if item.get("status") == "FAIL" else "MEDIUM",
                "criterion": item.get("criterion", ""),
                "status": item.get("status", "UNKNOWN"),
                "confidence_score": item.get("confidence_score", 50),
                "evidence": item.get("evidence", ""),
                "line_refs": item.get("line_refs", []),
                "assumption_gaps": item.get("assumption_gaps", []),
                "missing_coverage": item.get("missing_coverage", []),
            })

    security_findings = security_report.get("findings", []) if isinstance(security_report, dict) else []
    for finding in security_findings:
        overall_pass = False
        all_findings.append({
            "category": "SECURITY",
            "severity": finding.get("severity", "MEDIUM"),
            "type": finding.get("type", ""),
            "owasp_category": finding.get("owasp_category", ""),
            "line": finding.get("line", ""),
            "description": finding.get("description", ""),
            "attack_vector": finding.get("attack_vector", ""),
            "exploit_complexity": finding.get("exploit_complexity", ""),
            "cve_examples": finding.get("cve_examples", []),
            "fix": finding.get("fix", ""),
        })

    perf_findings = perf_report.get("findings", []) if isinstance(perf_report, dict) else []
    for finding in perf_findings:
        if finding.get("severity") in ("HIGH", "MEDIUM"):
            overall_pass = False
        all_findings.append({
            "category": "PERFORMANCE",
            "severity": finding.get("severity", "MEDIUM"),
            "type": finding.get("type", ""),
            "line": finding.get("line", ""),
            "description": finding.get("description", ""),
            "complexity_impact": finding.get("complexity_impact", ""),
            "scale_threshold": finding.get("scale_threshold", ""),
            "estimated_latency_impact": finding.get("estimated_latency_impact", ""),
            "fix": finding.get("fix", ""),
        })

    style_findings = style_report.get("findings", []) if isinstance(style_report, dict) else []
    for finding in style_findings:
        all_findings.append({
            "category": "STYLE",
            "severity": finding.get("severity", "LOW"),
            "type": finding.get("type", ""),
            "rule_ref": finding.get("rule_ref", ""),
            "line": finding.get("line", ""),
            "description": finding.get("description", ""),
            "maintainability_score_impact": finding.get("maintainability_score_impact", ""),
            "fix": finding.get("fix", ""),
        })

    if style_findings:
        overall_pass = False

    ac_verdict = ac_report.get("ac_verdict", {}) if isinstance(ac_report, dict) else {}
    security_verdict = security_report.get("ac_security_verdict", {}) if isinstance(security_report, dict) else {}
    perf_verdict = perf_report.get("ac_perf_verdict", {}) if isinstance(perf_report, dict) else {}
    style_verdict = style_report.get("ac_style_verdict", {}) if isinstance(style_report, dict) else {}

    security_sev = security_report.get("severity_summary", {}) if isinstance(security_report, dict) else {}

    gap_analysis = {
        "ac": ac_report.get("gap_analysis", {}) if isinstance(ac_report, dict) else {},
        "security": security_report.get("gap_analysis", {}) if isinstance(security_report, dict) else {},
        "performance": perf_report.get("gap_analysis", {}) if isinstance(perf_report, dict) else {},
        "style": style_report.get("gap_analysis", {}) if isinstance(style_report, dict) else {},
    }

    state["final_verdict"] = {
        "overall": "PASS" if overall_pass else "FAIL",
        "findings": all_findings,
        "gap_analysis": gap_analysis,
        "sub_verdicts": {
            "acceptance_criteria": ac_verdict,
            "security": security_verdict,
            "performance": perf_verdict,
            "style": style_verdict,
        },
        "summary": {
            "total_findings": len(all_findings),
            "security_findings": len(security_findings),
            "security_critical": security_sev.get("critical", 0),
            "security_high": security_sev.get("high", 0),
            "perf_findings": len(perf_findings),
            "style_findings": len(style_findings),
            "ac_items_evaluated": len(ac_criteria),
        },
    }
    return state


def _sev_icon(sev: str) -> str:
    return {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(sev.upper(), "⚪")


def _status_icon(status: str) -> str:
    return {"PASS": "✅", "FAIL": "❌", "PARTIAL": "⚠️", "UNVERIFIABLE": "❓"}.get(status.upper(), "❓")


def _build_markdown_comment(verdict: dict, pr_description: str) -> str:
    overall = verdict["overall"]
    summary = verdict["summary"]
    findings = verdict["findings"]
    gap_analysis = verdict.get("gap_analysis", {})
    sub_verdicts = verdict.get("sub_verdicts", {})

    header_icon = "✅" if overall == "PASS" else "❌"

    ac_status = sub_verdicts.get("acceptance_criteria", {}).get("status", "?")
    sec_status = sub_verdicts.get("security", {}).get("status", "?")
    perf_status = sub_verdicts.get("performance", {}).get("status", "?")
    style_status = sub_verdicts.get("style", {}).get("status", "?")

    sec_crit = summary.get("security_critical", 0)
    sec_high = summary.get("security_high", 0)
    sec_label = f"{summary['security_findings']} finding(s)"
    if sec_crit:
        sec_label += f" · 🔴 {sec_crit} CRITICAL"
    if sec_high:
        sec_label += f" · 🟠 {sec_high} HIGH"

    lines = [
        f"## {header_icon} GateCheck PR Review — **{overall}**",
        "",
        f"> {pr_description[:300]}{'...' if len(pr_description) > 300 else ''}",
        "",
        "### Dashboard",
        "",
        "| Agent | Status | Detail |",
        "|---|---|---|",
        f"| AC Verifier | {_status_icon(ac_status)} {ac_status} | {summary['ac_items_evaluated']} criteria evaluated |",
        f"| Security Auditor | {_status_icon(sec_status)} {sec_status} | {sec_label} |",
        f"| Perf Analyzer | {_status_icon(perf_status)} {perf_status} | {summary['perf_findings']} finding(s) |",
        f"| Style Enforcer | {_status_icon(style_status)} {style_status} | {summary['style_findings']} finding(s) |",
        "",
    ]

    # ── AC Criteria ────────────────────────────────────────────────────────────
    ac_findings = [f for f in findings if f.get("category") == "AC"]
    if ac_findings:
        lines += ["---", "### 📋 AC Verification", ""]
        lines += ["| Status | Confidence | Criterion | Evidence | Missing Coverage |", "|---|---|---|---|---|"]
        for f in ac_findings:
            st = _status_icon(f.get("status", ""))
            conf = f.get("confidence_score", "?")
            crit = str(f.get("criterion", "")).replace("|", "\\|")[:100]
            evid = str(f.get("evidence", "")).replace("|", "\\|")[:120]
            cov = "; ".join(f.get("missing_coverage", []))[:80] or "—"
            lines.append(f"| {st} {f.get('status','')} | {conf}% | {crit} | {evid} | {cov} |")
        lines.append("")

        ac_gap = gap_analysis.get("ac", {})
        ac_gap_items = (
            ac_gap.get("unverified_risks", [])
            + ac_gap.get("future_risks", [])
            + ac_gap.get("missing_coverage", [])
        )
        if ac_gap_items:
            lines += [
                "<details>",
                "<summary>🔍 AC Gap Analysis — risks that cannot be verified from the diff</summary>",
                "",
            ]
            if ac_gap.get("unverified_risks"):
                lines.append("**Unverified Risks** (require runtime/DB/external service validation):")
                for r in ac_gap["unverified_risks"]:
                    lines.append(f"- {r}")
                lines.append("")
            if ac_gap.get("future_risks"):
                lines.append("**Future Risks** (patterns that could break AC in later PRs):")
                for r in ac_gap["future_risks"]:
                    lines.append(f"- {r}")
                lines.append("")
            if ac_gap.get("missing_coverage"):
                lines.append("**Missing Test Coverage** (AC items with no test signal in diff):")
                for r in ac_gap["missing_coverage"]:
                    lines.append(f"- {r}")
                lines.append("")
            lines += ["</details>", ""]

    # ── Security ───────────────────────────────────────────────────────────────
    sec_findings = [f for f in findings if f.get("category") == "SECURITY"]
    lines += ["---", "### 🔐 Security Audit", ""]
    if sec_findings:
        lines += ["| Sev | OWASP | Location | Vulnerability | Attack Vector | Fix |", "|---|---|---|---|---|---|"]
        for f in sec_findings:
            sev_ico = _sev_icon(f.get("severity", ""))
            owasp = f.get("owasp_category", "—")[:30]
            line_ref = str(f.get("line", "—"))[:40]
            desc = str(f.get("description", "")).replace("|", "\\|")[:100]
            av = str(f.get("attack_vector", "")).replace("|", "\\|")[:80]
            fix = str(f.get("fix", "")).replace("|", "\\|")[:80]
            cves = f.get("cve_examples", [])
            cve_str = f" ({', '.join(cves[:2])})" if cves else ""
            lines.append(f"| {sev_ico} {f.get('severity','')} | {owasp} | `{line_ref}` | {desc}{cve_str} | {av} | {fix} |")
        lines.append("")
    else:
        lines += ["> No security vulnerabilities detected in this diff.", ""]

    sec_gap = gap_analysis.get("security", {})
    sec_gap_items = (
        sec_gap.get("untested_attack_surfaces", [])
        + sec_gap.get("dependency_risks", [])
        + sec_gap.get("future_vulnerabilities", [])
        + sec_gap.get("privilege_escalation_paths", [])
    )
    if sec_gap_items:
        lines += [
            "<details>",
            "<summary>🔍 Security Gap Analysis — attack surfaces and future risks not caught by diff alone</summary>",
            "",
        ]
        if sec_gap.get("untested_attack_surfaces"):
            lines.append("**Untested Attack Surfaces** (new endpoints/handlers with no visible authz):")
            for r in sec_gap["untested_attack_surfaces"]:
                lines.append(f"- {r}")
            lines.append("")
        if sec_gap.get("dependency_risks"):
            lines.append("**Dependency Risks** (new imports — check for CVEs and version pinning):")
            for r in sec_gap["dependency_risks"]:
                lines.append(f"- {r}")
            lines.append("")
        if sec_gap.get("future_vulnerabilities"):
            lines.append("**Future Vulnerabilities** (patterns that become exploitable as codebase grows):")
            for r in sec_gap["future_vulnerabilities"]:
                lines.append(f"- {r}")
            lines.append("")
        if sec_gap.get("privilege_escalation_paths"):
            lines.append("**Privilege Escalation Paths** (IAM/permissions beyond what PR requires):")
            for r in sec_gap["privilege_escalation_paths"]:
                lines.append(f"- {r}")
            lines.append("")
        lines += ["</details>", ""]

    # ── Performance ────────────────────────────────────────────────────────────
    perf_findings = [f for f in findings if f.get("category") == "PERFORMANCE"]
    if perf_findings:
        lines += ["---", "### ⚡ Performance Analysis", ""]
        lines += ["| Sev | Location | Issue | Complexity Impact | Scale Threshold | Fix |", "|---|---|---|---|---|---|"]
        for f in perf_findings:
            sev_ico = _sev_icon(f.get("severity", ""))
            line_ref = str(f.get("line", "—"))[:40]
            desc = str(f.get("description", "")).replace("|", "\\|")[:80]
            cx = str(f.get("complexity_impact", "")).replace("|", "\\|")[:40]
            scale = str(f.get("scale_threshold", "")).replace("|", "\\|")[:50]
            fix = str(f.get("fix", "")).replace("|", "\\|")[:60]
            lines.append(f"| {sev_ico} {f.get('severity','')} | `{line_ref}` | {desc} | {cx} | {scale} | {fix} |")
        lines.append("")

        perf_gap = gap_analysis.get("performance", {})
        perf_gap_items = (
            perf_gap.get("scalability_risks", [])
            + perf_gap.get("unmeasured_slas", [])
            + perf_gap.get("future_bottlenecks", [])
            + perf_gap.get("missing_observability", [])
        )
        if perf_gap_items:
            lines += [
                "<details>",
                "<summary>🔍 Performance Gap Analysis — scalability risks not visible in diff</summary>",
                "",
            ]
            for key, label in [
                ("scalability_risks", "Scalability Risks (fails at 10x/100x load)"),
                ("unmeasured_slas", "Unmeasured SLAs (SLA in AC but unverifiable from diff)"),
                ("future_bottlenecks", "Future Bottlenecks (hotspots as feature grows)"),
                ("missing_observability", "Missing Observability (no metrics/traces/profiling)"),
            ]:
                items = perf_gap.get(key, [])
                if items:
                    lines.append(f"**{label}:**")
                    for r in items:
                        lines.append(f"- {r}")
                    lines.append("")
            lines += ["</details>", ""]

    # ── Style ──────────────────────────────────────────────────────────────────
    style_findings = [f for f in findings if f.get("category") == "STYLE"]
    if style_findings:
        lines += ["---", "### 📝 Style & Maintainability", ""]
        lines += ["| Sev | Rule | Location | Issue | Impact | Fix |", "|---|---|---|---|---|---|"]
        for f in style_findings:
            sev_ico = _sev_icon(f.get("severity", ""))
            rule = str(f.get("rule_ref", "—"))[:20]
            line_ref = str(f.get("line", "—"))[:40]
            desc = str(f.get("description", "")).replace("|", "\\|")[:80]
            impact = str(f.get("maintainability_score_impact", "")).replace("|", "\\|")[:10]
            fix = str(f.get("fix", "")).replace("|", "\\|")[:60]
            lines.append(f"| {sev_ico} {f.get('severity','')} | `{rule}` | `{line_ref}` | {desc} | {impact} | {fix} |")
        lines.append("")

        style_gap = gap_analysis.get("style", {})
        style_gap_items = (
            style_gap.get("maintainability_risks", [])
            + style_gap.get("missing_contracts", [])
            + style_gap.get("test_coverage_gaps", [])
            + style_gap.get("tech_debt_introduced", [])
        )
        if style_gap_items:
            lines += [
                "<details>",
                "<summary>🔍 Style Gap Analysis — maintainability and tech debt risks</summary>",
                "",
            ]
            for key, label in [
                ("maintainability_risks", "Maintainability Risks"),
                ("missing_contracts", "Missing Type Contracts (hurt refactoring confidence)"),
                ("test_coverage_gaps", "Test Coverage Gaps"),
                ("tech_debt_introduced", "Tech Debt Introduced"),
            ]:
                items = style_gap.get(key, [])
                if items:
                    lines.append(f"**{label}:**")
                    for r in items:
                        lines.append(f"- {r}")
                    lines.append("")
            lines += ["</details>", ""]

    lines += [
        "---",
        "_Generated by [GateCheck](https://github.com/IlaakshMishra/gatecheck) — AWS Bedrock AgentCore · 4 specialized agents_",
    ]

    return "\n".join(lines)


def _build_graph() -> StateGraph:
    graph = StateGraph(OrchestratorState)
    graph.add_node("parse_ac", node_parse_ac)
    graph.add_node("categorize", node_categorize)
    graph.add_node("fan_out", node_fan_out)
    graph.add_node("synthesize", node_synthesize)

    graph.set_entry_point("parse_ac")
    graph.add_edge("parse_ac", "categorize")
    graph.add_edge("categorize", "fan_out")
    graph.add_edge("fan_out", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


_workflow = _build_graph()


@app.entrypoint
async def handler(payload: dict) -> dict:
    pr_diff = payload.get("pr_diff", "")
    acceptance_criteria = payload.get("acceptance_criteria", "")
    pr_description = payload.get("pr_description", "")
    repo_url = payload.get("repo_url", "")
    pr_number = int(payload.get("pr_number", 0))

    if not pr_diff:
        return {"error": "pr_diff is required"}
    if not repo_url:
        return {"error": "repo_url is required"}
    if not pr_number:
        return {"error": "pr_number is required"}

    initial_state: OrchestratorState = {
        "pr_diff": pr_diff,
        "acceptance_criteria": acceptance_criteria,
        "pr_description": pr_description,
        "repo_url": repo_url,
        "pr_number": pr_number,
        "ac_items": [],
        "ac_bucket": [],
        "security_bucket": [],
        "perf_bucket": [],
        "style_bucket": [],
        "ac_report": {},
        "security_report": {},
        "perf_report": {},
        "style_report": {},
        "final_verdict": {},
    }

    final_state = await _workflow.ainvoke(initial_state)
    verdict = final_state["final_verdict"]

    if repo_url and pr_number:
        try:
            comment_body = _build_markdown_comment(verdict, pr_description)
            _post_pr_comment(repo_url, pr_number, comment_body)
        except Exception as exc:
            logger.error("Failed to post GitHub comment: %s", exc)
            verdict["github_comment_error"] = str(exc)

    return verdict


if __name__ == "__main__":
    app.run()
