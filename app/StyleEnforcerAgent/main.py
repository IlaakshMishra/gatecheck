import json
import logging
import os
import re
from typing import Any, TypedDict

import boto3
from langchain_aws import ChatBedrockConverse
from langgraph.graph import END, StateGraph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

llm = ChatBedrockConverse(model_id=MODEL_ID)

BASE_STYLE_SYSTEM_PROMPT = """You are a strict Python style enforcer for code reviews.

Analyze the provided PR diff for the following style issues:
1. Naming conventions — functions/methods must use snake_case, classes must use PascalCase, constants must use UPPER_SNAKE_CASE
2. Dead code — unreachable code after return/raise, commented-out blocks of code, unused imports, unused variables
3. Missing type hints — public functions and methods must have parameter and return type annotations
4. Missing docstrings — public functions, classes, and methods (those not prefixed with _) must have docstrings
5. Import order violations — imports must follow PEP 8 order: stdlib → third-party → local, each group separated by a blank line

For each finding, cite the specific diff line or hunk.
Also evaluate the style-related acceptance criteria items.

{team_style_addendum}

Return ONLY valid JSON, no prose. Return this exact structure:
{{
  "findings": [
    {{
      "type": "<issue class>",
      "severity": "MEDIUM|LOW",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong>",
      "fix": "<how to fix it>"
    }}
  ],
  "ac_style_verdict": {{
    "status": "PASS|FAIL",
    "evaluated_items": [
      {{
        "criterion": "<criterion text>",
        "status": "PASS|FAIL|PARTIAL|UNVERIFIABLE",
        "evidence": "<explanation>"
      }}
    ]
  }}
}}

If no findings exist, return an empty array for findings and PASS for ac_style_verdict.
"""


def _load_team_style_guide() -> str:
    if not MEMORY_ID:
        return ""
    try:
        bedrock_agent_client = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
        response = bedrock_agent_client.retrieve(
            knowledgeBaseId=MEMORY_ID,
            retrievalQuery={"text": "team style guide coding standards conventions"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 5}
            },
        )
        results = response.get("retrievalResults", [])
        if not results:
            return ""
        snippets = []
        for r in results:
            content = r.get("content", {})
            text = content.get("text", "")
            if text:
                snippets.append(text)
        if not snippets:
            return ""
        combined = "\n\n".join(snippets[:3])
        return f"\nAdditional team style guide rules:\n{combined}\n"
    except Exception as exc:
        logger.warning("Could not load team style guide from memory %s: %s", MEMORY_ID, exc)
        return ""


class StyleEnforcerState(TypedDict):
    pr_diff: str
    ac_style_items: list[str]
    team_style_guide: str
    result: dict


def node_load_style_guide(state: StyleEnforcerState) -> StyleEnforcerState:
    state["team_style_guide"] = _load_team_style_guide()
    return state


def node_enforce(state: StyleEnforcerState) -> StyleEnforcerState:
    pr_diff = state["pr_diff"]
    ac_items = state["ac_style_items"]
    team_addendum = state.get("team_style_guide", "")

    system_prompt = BASE_STYLE_SYSTEM_PROMPT.format(
        team_style_addendum=team_addendum
    )

    human_message = (
        f"PR Diff to check for style issues:\n```\n{pr_diff}\n```\n\n"
        f"Style-related acceptance criteria to evaluate:\n{json.dumps(ac_items, indent=2)}"
    )

    response = llm.invoke([
        {"role": "system", "content": system_prompt},
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
    if "ac_style_verdict" not in parsed:
        parsed["ac_style_verdict"] = {
            "status": "PASS" if not parsed["findings"] else "FAIL",
            "evaluated_items": [],
        }

    findings = parsed["findings"]
    if findings:
        parsed["ac_style_verdict"]["status"] = "FAIL"

    ac_verdict = parsed["ac_style_verdict"]
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
        "ac_style_verdict": {
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
    graph = StateGraph(StyleEnforcerState)
    graph.add_node("load_style_guide", node_load_style_guide)
    graph.add_node("enforce", node_enforce)
    graph.set_entry_point("load_style_guide")
    graph.add_edge("load_style_guide", "enforce")
    graph.add_edge("enforce", END)
    return graph.compile()


_workflow = _build_graph()


def run_style_enforcement(pr_diff: str, ac_style_items: list[str]) -> dict:
    initial: StyleEnforcerState = {
        "pr_diff": pr_diff,
        "ac_style_items": ac_style_items,
        "team_style_guide": "",
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

    class StyleEnforcerExecutor(AgentExecutor):
        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.submit()
            await updater.start_work()

            try:
                user_message = context.get_user_input()
                try:
                    payload = json.loads(user_message)
                except (json.JSONDecodeError, TypeError):
                    payload = {"pr_diff": str(user_message), "ac_style_items": []}

                pr_diff = payload.get("pr_diff", "")
                ac_style_items = payload.get("ac_style_items", [])

                result = run_style_enforcement(pr_diff, ac_style_items)
                output_text = json.dumps(result)

                await updater.add_artifact(
                    [TextPart(text=output_text)],
                    name="style_enforcement_result",
                )
                await updater.complete()
            except Exception as exc:
                logger.error("StyleEnforcerExecutor error: %s", exc)
                await updater.failed()

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.failed()

    agent_card = AgentCard(
        name="StyleEnforcerAgent",
        description=(
            "Enforces Python style standards on PR diffs. Checks naming conventions (snake_case/PascalCase), "
            "dead code, missing type hints, missing docstrings on public functions, and import order (PEP 8). "
            "Optionally loads team style guide from Bedrock Knowledge Base via MEMORY_ID env var."
        ),
        url="http://localhost:9000",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="style_enforcement",
                name="Style Enforcement",
                description="Check a PR diff for Python style violations",
                tags=["pr-review", "style", "linting", "naming", "type-hints", "docstrings"],
                examples=[
                    '{"pr_diff": "...", "ac_style_items": ["All public functions must have docstrings"]}'
                ],
            )
        ],
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
    )

    if __name__ == "__main__":
        serve_a2a(StyleEnforcerExecutor(), agent_card)

except ImportError:
    try:
        import uvicorn
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.events import EventQueue
        from a2a.server.tasks import TaskUpdater
        from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TextPart

        class StyleEnforcerExecutor(AgentExecutor):
            async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.submit()
                await updater.start_work()

                try:
                    user_message = context.get_user_input()
                    try:
                        payload = json.loads(user_message)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"pr_diff": str(user_message), "ac_style_items": []}

                    pr_diff = payload.get("pr_diff", "")
                    ac_style_items = payload.get("ac_style_items", [])

                    result = run_style_enforcement(pr_diff, ac_style_items)
                    output_text = json.dumps(result)

                    await updater.add_artifact(
                        [TextPart(text=output_text)],
                        name="style_enforcement_result",
                    )
                    await updater.complete()
                except Exception as exc:
                    logger.error("StyleEnforcerExecutor error: %s", exc)
                    await updater.failed()

            async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.failed()

        agent_card = AgentCard(
            name="StyleEnforcerAgent",
            description=(
                "Enforces Python style standards on PR diffs. Checks naming conventions (snake_case/PascalCase), "
                "dead code, missing type hints, missing docstrings on public functions, and import order (PEP 8). "
                "Optionally loads team style guide from Bedrock Knowledge Base via MEMORY_ID env var."
            ),
            url="http://localhost:9000",
            version="1.0.0",
            capabilities=AgentCapabilities(streaming=False),
            skills=[
                AgentSkill(
                    id="style_enforcement",
                    name="Style Enforcement",
                    description="Check a PR diff for Python style violations",
                    tags=["pr-review", "style", "linting", "naming", "type-hints", "docstrings"],
                    examples=[
                        '{"pr_diff": "...", "ac_style_items": ["All public functions must have docstrings"]}'
                    ],
                )
            ],
            defaultInputModes=["application/json"],
            defaultOutputModes=["application/json"],
        )

        if __name__ == "__main__":
            a2a_app = A2AStarletteApplication(
                agent_card=agent_card,
                executor=StyleEnforcerExecutor(),
            )
            uvicorn.run(a2a_app.build(), host="0.0.0.0", port=9000)

    except ImportError as imp_err:
        logger.warning("A2A server libraries not available: %s", imp_err)

        if __name__ == "__main__":
            import sys
            payload = json.load(sys.stdin)
            result = run_style_enforcement(
                payload.get("pr_diff", ""),
                payload.get("ac_style_items", []),
            )
            print(json.dumps(result, indent=2))
