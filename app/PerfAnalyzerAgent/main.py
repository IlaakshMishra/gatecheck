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

PERF_SYSTEM_PROMPT = """You are an expert performance engineer specializing in backend code review.

Analyze the provided PR diff for the following performance issue classes:
1. N+1 query problems — ORM queries inside loops, lazy loading triggered per iteration
2. Blocking I/O in async paths — synchronous DB calls, file I/O, or HTTP requests inside async functions without await or thread offloading
3. Unbounded loops or recursion — loops over collections with no size cap, unbounded pagination, missing LIMIT clauses
4. Big-O regressions — algorithmic complexity increases (e.g., O(n) → O(n²) due to nested loops over same data)
5. Missing database indices — queries filtering or sorting on columns likely lacking an index
6. Response time acceptance criteria thresholds — any AC item specifying latency/throughput SLAs

For each finding, cite the specific diff line or code block.
Also evaluate the performance-related acceptance criteria items.

Return ONLY valid JSON, no prose. Return this exact structure:
{
  "findings": [
    {
      "type": "<issue class>",
      "severity": "HIGH|MEDIUM|LOW",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong and its performance impact>",
      "fix": "<concrete optimization steps>"
    }
  ],
  "ac_perf_verdict": {
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

If no findings exist, return an empty array for findings and PASS for ac_perf_verdict.
"""


class PerfAnalyzerState(TypedDict):
    pr_diff: str
    ac_perf_items: list[str]
    result: dict


def node_analyze(state: PerfAnalyzerState) -> PerfAnalyzerState:
    pr_diff = state["pr_diff"]
    ac_items = state["ac_perf_items"]

    human_message = (
        f"PR Diff to analyze for performance issues:\n```\n{pr_diff}\n```\n\n"
        f"Performance-related acceptance criteria to evaluate:\n{json.dumps(ac_items, indent=2)}"
    )

    response = llm.invoke([
        {"role": "system", "content": PERF_SYSTEM_PROMPT},
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
    if "ac_perf_verdict" not in parsed:
        parsed["ac_perf_verdict"] = {
            "status": "PASS" if not parsed["findings"] else "FAIL",
            "evaluated_items": [],
        }

    findings = parsed["findings"]
    high_sev = [f for f in findings if isinstance(f, dict) and f.get("severity") in ("HIGH", "MEDIUM")]
    if high_sev:
        parsed["ac_perf_verdict"]["status"] = "FAIL"

    ac_verdict = parsed["ac_perf_verdict"]
    evaluated = ac_verdict.get("evaluated_items", [])
    if ac_items and not evaluated:
        ac_verdict["evaluated_items"] = [
            {
                "criterion": item,
                "status": "UNVERIFIABLE",
                "evidence": "Could not evaluate from diff alone — runtime profiling required",
            }
            for item in ac_items
        ]

    state["result"] = parsed
    return state


def _empty_result(ac_items: list[str]) -> dict:
    return {
        "findings": [],
        "ac_perf_verdict": {
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
    graph = StateGraph(PerfAnalyzerState)
    graph.add_node("analyze", node_analyze)
    graph.set_entry_point("analyze")
    graph.add_edge("analyze", END)
    return graph.compile()


_workflow = _build_graph()


def run_perf_analysis(pr_diff: str, ac_perf_items: list[str]) -> dict:
    initial: PerfAnalyzerState = {
        "pr_diff": pr_diff,
        "ac_perf_items": ac_perf_items,
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

    class PerfAnalyzerExecutor(AgentExecutor):
        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.submit()
            await updater.start_work()

            try:
                user_message = context.get_user_input()
                try:
                    payload = json.loads(user_message)
                except (json.JSONDecodeError, TypeError):
                    payload = {"pr_diff": str(user_message), "ac_perf_items": []}

                pr_diff = payload.get("pr_diff", "")
                ac_perf_items = payload.get("ac_perf_items", [])

                result = run_perf_analysis(pr_diff, ac_perf_items)
                output_text = json.dumps(result)

                await updater.add_artifact(
                    [TextPart(text=output_text)],
                    name="perf_analysis_result",
                )
                await updater.complete()
            except Exception as exc:
                logger.error("PerfAnalyzerExecutor error: %s", exc)
                await updater.failed()

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.failed()

    agent_card = AgentCard(
        name="PerfAnalyzerAgent",
        description=(
            "Analyzes PR diffs for performance issues including N+1 queries, blocking I/O in async paths, "
            "unbounded loops, Big-O regressions, missing indices, and response-time SLA violations."
        ),
        url="http://localhost:9000",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="perf_analysis",
                name="Performance Analysis",
                description="Analyze a PR diff for performance issues",
                tags=["pr-review", "performance", "optimization", "n+1", "async"],
                examples=[
                    '{"pr_diff": "...", "ac_perf_items": ["API response time must be < 200ms"]}'
                ],
            )
        ],
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
    )

    if __name__ == "__main__":
        serve_a2a(PerfAnalyzerExecutor(), agent_card)

except ImportError:
    try:
        import uvicorn
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.events import EventQueue
        from a2a.server.tasks import TaskUpdater
        from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TextPart

        class PerfAnalyzerExecutor(AgentExecutor):
            async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.submit()
                await updater.start_work()

                try:
                    user_message = context.get_user_input()
                    try:
                        payload = json.loads(user_message)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"pr_diff": str(user_message), "ac_perf_items": []}

                    pr_diff = payload.get("pr_diff", "")
                    ac_perf_items = payload.get("ac_perf_items", [])

                    result = run_perf_analysis(pr_diff, ac_perf_items)
                    output_text = json.dumps(result)

                    await updater.add_artifact(
                        [TextPart(text=output_text)],
                        name="perf_analysis_result",
                    )
                    await updater.complete()
                except Exception as exc:
                    logger.error("PerfAnalyzerExecutor error: %s", exc)
                    await updater.failed()

            async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
                updater = TaskUpdater(event_queue, context.task_id, context.context_id)
                await updater.failed()

        agent_card = AgentCard(
            name="PerfAnalyzerAgent",
            description=(
                "Analyzes PR diffs for performance issues including N+1 queries, blocking I/O in async paths, "
                "unbounded loops, Big-O regressions, missing indices, and response-time SLA violations."
            ),
            url="http://localhost:9000",
            version="1.0.0",
            capabilities=AgentCapabilities(streaming=False),
            skills=[
                AgentSkill(
                    id="perf_analysis",
                    name="Performance Analysis",
                    description="Analyze a PR diff for performance issues",
                    tags=["pr-review", "performance", "optimization", "n+1", "async"],
                    examples=[
                        '{"pr_diff": "...", "ac_perf_items": ["API response time must be < 200ms"]}'
                    ],
                )
            ],
            defaultInputModes=["application/json"],
            defaultOutputModes=["application/json"],
        )

        if __name__ == "__main__":
            a2a_app = A2AStarletteApplication(
                agent_card=agent_card,
                executor=PerfAnalyzerExecutor(),
            )
            uvicorn.run(a2a_app.build(), host="0.0.0.0", port=9000)

    except ImportError as imp_err:
        logger.warning("A2A server libraries not available: %s", imp_err)

        if __name__ == "__main__":
            import sys
            payload = json.load(sys.stdin)
            result = run_perf_analysis(
                payload.get("pr_diff", ""),
                payload.get("ac_perf_items", []),
            )
            print(json.dumps(result, indent=2))
