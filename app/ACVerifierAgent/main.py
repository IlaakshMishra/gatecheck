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

AC_SYSTEM_PROMPT = """You are a strict acceptance-criteria verifier for code reviews.

For each acceptance criterion provided, examine the PR diff carefully and determine:
- PASS: The diff clearly implements the criterion correctly
- FAIL: The diff does not implement the criterion or implements it incorrectly
- PARTIAL: The diff partially implements the criterion but is incomplete
- UNVERIFIABLE: Cannot determine from the diff alone (requires runtime, DB state, etc.)

For each criterion:
1. Cite exact diff line or hunk references as evidence
2. Assign confidence_score 0-100 (100 = certain, 0 = pure guess)
3. List assumption_gaps — unstated assumptions that if wrong would flip your verdict
4. List missing_coverage — test/verification signals that SHOULD appear in the diff but don't

After evaluating all criteria, produce a gap_analysis:
- unverified_risks: requirements the diff implies but cannot prove (env config, migrations, external services)
- future_risks: code patterns introduced that could break acceptance criteria in subsequent PRs
- missing_coverage: AC items with zero test coverage evidence in the diff

Be precise and exhaustive. Treat absence of test coverage as a gap even if the criterion itself passes.

Return ONLY valid JSON, no prose. Exact structure:
{
  "criteria": [
    {
      "criterion": "<original criterion text>",
      "status": "PASS|FAIL|PARTIAL|UNVERIFIABLE",
      "confidence_score": <0-100>,
      "evidence": "<specific explanation referencing diff line/hunk>",
      "line_refs": ["<file:line or hunk reference>"],
      "assumption_gaps": ["<assumption that if wrong changes the verdict>"],
      "missing_coverage": ["<test or verification signal absent from diff>"]
    }
  ],
  "gap_analysis": {
    "unverified_risks": ["<AC requirement the diff implies but cannot prove>"],
    "future_risks": ["<pattern introduced that could break AC in a later PR>"],
    "missing_coverage": ["<AC item with no test coverage signals in the diff>"]
  }
}
"""


class ACVerifierState(TypedDict):
    pr_diff: str
    acceptance_criteria_list: list[str]
    result: dict


def node_verify(state: ACVerifierState) -> ACVerifierState:
    pr_diff = state["pr_diff"]
    criteria = state["acceptance_criteria_list"]

    human_message = (
        f"PR Diff:\n```\n{pr_diff}\n```\n\n"
        f"Acceptance Criteria to verify:\n{json.dumps(criteria, indent=2)}"
    )

    response = llm.invoke([
        {"role": "system", "content": AC_SYSTEM_PROMPT},
        {"role": "user", "content": human_message},
    ])

    raw = response.content if hasattr(response, "content") else str(response)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        obj_match = re.search(r"\{.*\}", raw, re.DOTALL)
        arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if obj_match:
            parsed = json.loads(obj_match.group(0))
        elif arr_match:
            parsed = {"criteria": json.loads(arr_match.group(0))}
        else:
            parsed = {
                "criteria": [
                    {
                        "criterion": c,
                        "status": "UNVERIFIABLE",
                        "evidence": "LLM returned non-parseable output",
                        "line_refs": [],
                    }
                    for c in criteria
                ]
            }

    if isinstance(parsed, list):
        parsed = {"criteria": parsed}

    criteria_results = parsed.get("criteria", [])
    any_fail = any(
        item.get("status") in ("FAIL", "PARTIAL")
        for item in criteria_results
        if isinstance(item, dict)
    )

    gap_analysis = parsed.get("gap_analysis", {
        "unverified_risks": [],
        "future_risks": [],
        "missing_coverage": [],
    })

    state["result"] = {
        "criteria": criteria_results,
        "gap_analysis": gap_analysis,
        "ac_verdict": {
            "status": "FAIL" if any_fail else "PASS",
            "total": len(criteria_results),
            "passed": sum(1 for i in criteria_results if isinstance(i, dict) and i.get("status") == "PASS"),
            "failed": sum(1 for i in criteria_results if isinstance(i, dict) and i.get("status") == "FAIL"),
            "partial": sum(1 for i in criteria_results if isinstance(i, dict) and i.get("status") == "PARTIAL"),
            "unverifiable": sum(1 for i in criteria_results if isinstance(i, dict) and i.get("status") == "UNVERIFIABLE"),
            "avg_confidence": round(
                sum(i.get("confidence_score", 50) for i in criteria_results if isinstance(i, dict))
                / max(len(criteria_results), 1)
            ),
        },
    }
    return state


def _build_graph() -> Any:
    graph = StateGraph(ACVerifierState)
    graph.add_node("verify", node_verify)
    graph.set_entry_point("verify")
    graph.add_edge("verify", END)
    return graph.compile()


_workflow = _build_graph()


def run_verification(pr_diff: str, acceptance_criteria_list: list[str]) -> dict:
    initial: ACVerifierState = {
        "pr_diff": pr_diff,
        "acceptance_criteria_list": acceptance_criteria_list,
        "result": {},
    }
    final = _workflow.invoke(initial)
    return final["result"]


from bedrock_agentcore.runtime import BedrockAgentCoreApp

_app = BedrockAgentCoreApp()


@_app.entrypoint
async def handler(payload: dict) -> dict:
    pr_diff = payload.get("pr_diff", "")
    criteria_list = payload.get("acceptance_criteria_list", [])
    return run_verification(pr_diff, criteria_list)


if __name__ == "__main__":
    _app.run()
