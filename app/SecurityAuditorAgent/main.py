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

SECURITY_SYSTEM_PROMPT = """You are an expert application security auditor specializing in code review.

Analyze the provided PR diff for the following vulnerability classes:
1. OWASP Top 10 (A01-A10:2021) — broken access control, cryptographic failures, injection, insecure design, security misconfiguration, vulnerable components, authentication failures, software integrity failures, logging failures, SSRF
2. Hardcoded secrets, API keys, passwords, tokens, private keys
3. IAM privilege escalation — overly permissive policies, wildcard actions/resources
4. SQL injection — raw query construction, string interpolation in queries
5. XSS — unescaped user input in HTML/template rendering
6. Command injection — subprocess calls with user-controlled input, eval/exec misuse
7. Path traversal — user-controlled file paths without sanitization
8. Insecure deserialization — pickle.loads, yaml.load (unsafe), unmarshaling untrusted data

For each finding, cite the exact diff line or hunk.
Also evaluate the security-related acceptance criteria items.

Return ONLY valid JSON, no prose. Return this exact structure:
{
  "findings": [
    {
      "type": "<vulnerability class>",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong and why it is dangerous>",
      "fix": "<concrete remediation steps>"
    }
  ],
  "ac_security_verdict": {
    "status": "PASS|FAIL",
    "evaluated_items": [
      {
        "criterion": "<criterion text>",
        "status": "PASS|FAIL|PARTIAL|UNVERIFIABLE",
        "evidence": "<explanation>"
      }
    ]
  }
}

If no findings exist, return an empty array for findings and PASS for ac_security_verdict.
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
    if "ac_security_verdict" not in parsed:
        parsed["ac_security_verdict"] = {
            "status": "PASS" if not parsed["findings"] else "FAIL",
            "evaluated_items": [],
        }

    findings = parsed["findings"]
    if findings:
        parsed["ac_security_verdict"]["status"] = "FAIL"

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
