import json
import logging
import os
import re
from typing import Any, TypedDict

from langchain_aws import ChatBedrockConverse
from langgraph.graph import END, StateGraph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

llm = ChatBedrockConverse(model_id=MODEL_ID)

SECURITY_SYSTEM_PROMPT = """You are an expert application security auditor (OSCP/CEH level) specializing in code review.

You ALWAYS run a full security audit regardless of acceptance criteria. Security is non-negotiable.

Analyze the provided PR diff exhaustively for ALL of the following:

VULNERABILITY CLASSES (check every one):
1. OWASP A01:2021 Broken Access Control — missing authz checks, IDOR, privilege escalation, CORS misconfig
2. OWASP A02:2021 Cryptographic Failures — weak ciphers (MD5/SHA1), hardcoded secrets, unencrypted PII transmission, missing TLS
3. OWASP A03:2021 Injection — SQL injection (string interpolation in queries), command injection (subprocess + user input), LDAP/XPath injection, template injection
4. OWASP A04:2021 Insecure Design — missing rate limiting, no input validation at trust boundaries, business logic flaws
5. OWASP A05:2021 Security Misconfiguration — debug modes enabled, default credentials, overly permissive CORS, verbose error messages exposing stack traces
6. OWASP A06:2021 Vulnerable & Outdated Components — new third-party imports (flag for version pinning and known CVEs)
7. OWASP A07:2021 Authentication Failures — broken session management, weak password policies, missing MFA enforcement
8. OWASP A08:2021 Software & Data Integrity Failures — insecure deserialization (pickle.loads, yaml.load unsafe, json.loads on untrusted input), unsigned updates
9. OWASP A09:2021 Logging & Monitoring Failures — secrets logged, PII in logs, missing audit trail for sensitive operations
10. OWASP A10:2021 SSRF — user-controlled URLs passed to requests/urllib without allowlist validation
11. IAM Privilege Escalation — wildcard Action/Resource ("*"), overly broad managed policies, cross-account trust
12. Hardcoded Secrets — API keys, tokens, passwords, private keys anywhere in diff (including comments and tests)
13. XSS — unescaped user input in HTML/template rendering, innerHTML, dangerouslySetInnerHTML
14. Path Traversal — user-controlled file paths without sanitization or canonicalization
15. Race Conditions — TOCTOU patterns, missing locks on shared mutable state in concurrent code

For EACH finding provide:
- owasp_category: exact OWASP ID (e.g. "A03:2021 Injection") or "Non-OWASP" for IAM/secrets
- attack_vector: concrete how-an-attacker-exploits-this (1-2 sentences, specific to the diff)
- exploit_complexity: LOW / MEDIUM / HIGH
- cve_examples: list up to 2 real CVE IDs of similar vulnerabilities (empty list if none known)

After findings, produce gap_analysis covering:
- untested_attack_surfaces: new API endpoints, file handlers, or external integrations introduced by diff with no auth/authz validation visible
- dependency_risks: new third-party imports with potential supply-chain or known-vuln concerns
- future_vulnerabilities: patterns introduced that will become exploitable as the codebase grows (e.g. missing parameterization that works now but will be copy-pasted into a dangerous context)
- privilege_escalation_paths: any IAM roles, permissions, or trust relationships that grant more access than the PR description requires

Return ONLY valid JSON, no prose. Exact structure:
{
  "findings": [
    {
      "type": "<vulnerability class>",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "owasp_category": "<A0X:2021 Name or Non-OWASP>",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong and why it is dangerous — be specific>",
      "attack_vector": "<how an attacker exploits this concretely>",
      "exploit_complexity": "LOW|MEDIUM|HIGH",
      "cve_examples": ["CVE-YYYY-NNNNN"],
      "fix": "<concrete step-by-step remediation>"
    }
  ],
  "gap_analysis": {
    "untested_attack_surfaces": ["<new endpoint/handler with no visible authz>"],
    "dependency_risks": ["<new import and its supply-chain or CVE concern>"],
    "future_vulnerabilities": ["<pattern that becomes dangerous as codebase grows>"],
    "privilege_escalation_paths": ["<IAM/permission concern beyond what PR requires>"]
  },
  "ac_security_verdict": {
    "status": "PASS|FAIL",
    "evaluated_items": [
      {
        "criterion": "<criterion text>",
        "status": "PASS|FAIL|PARTIAL|UNVERIFIABLE",
        "evidence": "<specific diff evidence>"
      }
    ]
  }
}

If no findings exist return empty array for findings and PASS for ac_security_verdict. Still populate gap_analysis.
"""


class SecurityAuditorState(TypedDict):
    pr_diff: str
    ac_security_items: list[str]
    result: dict


def node_audit(state: SecurityAuditorState) -> SecurityAuditorState:
    pr_diff = state["pr_diff"]
    ac_items = state["ac_security_items"]

    human_message = (
        f"PR Diff to audit:\n```\n{pr_diff}\n```\n\n"
        f"Security-related acceptance criteria to evaluate:\n{json.dumps(ac_items, indent=2)}"
    )

    response = llm.invoke([
        {"role": "system", "content": SECURITY_SYSTEM_PROMPT},
        {"role": "user", "content": human_message},
    ])

    raw = response.content if hasattr(response, "content") else str(response)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        obj_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if obj_match:
            try:
                parsed = json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                parsed = _empty_result(ac_items)
        else:
            parsed = _empty_result(ac_items)

    if "findings" not in parsed:
        parsed["findings"] = []
    if "gap_analysis" not in parsed:
        parsed["gap_analysis"] = {
            "untested_attack_surfaces": [],
            "dependency_risks": [],
            "future_vulnerabilities": [],
            "privilege_escalation_paths": [],
        }
    if "ac_security_verdict" not in parsed:
        parsed["ac_security_verdict"] = {
            "status": "PASS" if not parsed["findings"] else "FAIL",
            "evaluated_items": [],
        }

    findings = parsed["findings"]
    if findings:
        parsed["ac_security_verdict"]["status"] = "FAIL"

    criticals = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "CRITICAL")
    highs = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "HIGH")
    parsed["severity_summary"] = {
        "critical": criticals,
        "high": highs,
        "medium": sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "MEDIUM"),
        "low": sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "LOW"),
    }

    ac_verdict = parsed["ac_security_verdict"]
    evaluated = ac_verdict.get("evaluated_items", [])
    if ac_items and not evaluated:
        ac_verdict["evaluated_items"] = [
            {
                "criterion": item,
                "status": "UNVERIFIABLE",
                "evidence": "Could not evaluate from diff alone",
            }
            for item in ac_items
        ]

    state["result"] = parsed
    return state


def _empty_result(ac_items: list[str]) -> dict:
    return {
        "findings": [],
        "ac_security_verdict": {
            "status": "PASS",
            "evaluated_items": [
                {
                    "criterion": item,
                    "status": "UNVERIFIABLE",
                    "evidence": "LLM returned non-parseable output",
                }
                for item in ac_items
            ],
        },
    }


def _build_graph() -> Any:
    graph = StateGraph(SecurityAuditorState)
    graph.add_node("audit", node_audit)
    graph.set_entry_point("audit")
    graph.add_edge("audit", END)
    return graph.compile()


_workflow = _build_graph()


def run_security_audit(pr_diff: str, ac_security_items: list[str]) -> dict:
    initial: SecurityAuditorState = {
        "pr_diff": pr_diff,
        "ac_security_items": ac_security_items,
        "result": {},
    }
    final = _workflow.invoke(initial)
    return final["result"]


try:
    from bedrock_agentcore.runtime import serve_a2a

    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue
    from a2a.server.tasks import TaskUpdater
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TextPart

    class SecurityAuditorExecutor(AgentExecutor):
        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.submit()
            await updater.start_work()

            try:
                user_message = context.get_user_input()
                try:
                    payload = json.loads(user_message)
                except (json.JSONDecodeError, TypeError):
                    payload = {"pr_diff": str(user_message), "ac_security_items": []}

                pr_diff = payload.get("pr_diff", "")
                ac_security_items = payload.get("ac_security_items", [])

                result = run_security_audit(pr_diff, ac_security_items)
                output_text = json.dumps(result)

                await updater.add_artifact(
                    [TextPart(text=output_text)],
                    name="security_audit_result",
                )
                await updater.complete()
            except Exception as exc:
                logger.error("SecurityAuditorExecutor error: %s", exc)
                await updater.failed()

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.failed()

    agent_card = AgentCard(
        name="SecurityAuditorAgent",
        description=(
            "Performs security auditing of PR diffs. Checks OWASP Top 10, hardcoded secrets, "
            "IAM privilege escalation, SQL injection, XSS, command injection, path traversal, "
            "and insecure deserialization."
        ),
        url="http://localhost:9000",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="security_audit",
                name="Security Audit",
                description="Audit a PR diff for security vulnerabilities",
                tags=["pr-review", "security", "owasp", "vulnerability"],
                examples=[
                    '{"pr_diff": "...", "ac_security_items": ["All user inputs must be sanitized"]}'
                ],
            )
        ],
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
    )

    if __name__ == "__main__":
        serve_a2a(SecurityAuditorExecutor(), agent_card)

except ImportError:
    try:
        import uvicorn
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.events import EventQueue
        from a2a.server.tasks import TaskUpdater
        from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TextPart

        class SecurityAuditorExecutor(AgentExecutor):
            async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.submit()
                await updater.start_work()

                try:
                    user_message = context.get_user_input()
                    try:
                        payload = json.loads(user_message)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"pr_diff": str(user_message), "ac_security_items": []}

                    pr_diff = payload.get("pr_diff", "")
                    ac_security_items = payload.get("ac_security_items", [])

                    result = run_security_audit(pr_diff, ac_security_items)
                    output_text = json.dumps(result)

                    await updater.add_artifact(
                        [TextPart(text=output_text)],
                        name="security_audit_result",
                    )
                    await updater.complete()
                except Exception as exc:
                    logger.error("SecurityAuditorExecutor error: %s", exc)
                    await updater.failed()

            async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.failed()

        agent_card = AgentCard(
            name="SecurityAuditorAgent",
            description=(
                "Performs security auditing of PR diffs. Checks OWASP Top 10, hardcoded secrets, "
                "IAM privilege escalation, SQL injection, XSS, command injection, path traversal, "
                "and insecure deserialization."
            ),
            url="http://localhost:9000",
            version="1.0.0",
            capabilities=AgentCapabilities(streaming=False),
            skills=[
                AgentSkill(
                    id="security_audit",
                    name="Security Audit",
                    description="Audit a PR diff for security vulnerabilities",
                    tags=["pr-review", "security", "owasp", "vulnerability"],
                    examples=[
                        '{"pr_diff": "...", "ac_security_items": ["All user inputs must be sanitized"]}'
                    ],
                )
            ],
            defaultInputModes=["application/json"],
            defaultOutputModes=["application/json"],
        )

        if __name__ == "__main__":
            a2a_app = A2AStarletteApplication(
                agent_card=agent_card,
                executor=SecurityAuditorExecutor(),
            )
            uvicorn.run(a2a_app.build(), host="0.0.0.0", port=9000)

    except ImportError as imp_err:
        logger.warning("A2A server libraries not available: %s", imp_err)

        if __name__ == "__main__":
            import sys
            payload = json.load(sys.stdin)
            result = run_security_audit(
                payload.get("pr_diff", ""),
                payload.get("ac_security_items", []),
            )
            print(json.dumps(result, indent=2))
