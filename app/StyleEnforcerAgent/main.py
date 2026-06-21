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

BASE_STYLE_SYSTEM_PROMPT = """You are a strict Python style enforcer for code reviews (PEP8 / mypy / ruff expert level).

Analyze the provided PR diff for ALL of the following:

MANDATORY CHECKS:
1. Naming conventions — snake_case for functions/methods/variables, PascalCase for classes, UPPER_SNAKE_CASE for module-level constants; flag every violation with exact line
2. Dead code — unreachable code after return/raise, commented-out blocks (not explanatory comments), unused imports (check if the symbol appears anywhere in the diff), unused variables (assigned but never read)
3. Missing type hints — every public function/method (not prefixed with _) must have: typed parameters, return type annotation, TypedDict or dataclass for complex return shapes
4. Missing docstrings — every public function, class, and method (not prefixed with _) must have a docstring; one-liner is sufficient but must exist
5. Import order violations — PEP 8: stdlib → third-party → local, blank line between each group; flag relative imports that should be absolute
6. Magic numbers/strings — bare literals (numbers not named, strings not in constants) in logic paths; flag with suggested constant name
7. Exception handling anti-patterns — bare `except:`, `except Exception: pass`, catching and silently swallowing exceptions
8. Mutable default arguments — `def f(x=[])` or `def f(x={})` patterns
9. Long functions — functions > 50 lines (flag with actual line count and suggestion to decompose)
10. God objects / oversized modules — classes with > 10 public methods or modules > 300 lines in the diff

For each finding:
- rule_ref: the PEP8/mypy/ruff rule reference (e.g. "PEP8-E302", "mypy-return-type", "ruff-B006")
- maintainability_score_impact: "HIGH" if this will cause bugs at refactor time, "MEDIUM" if it slows comprehension, "LOW" if cosmetic

After findings, produce gap_analysis:
- maintainability_risks: patterns that will cause breakage or confusion as the codebase grows
- missing_contracts: public functions lacking type hints that will hurt static analysis and refactoring confidence
- test_coverage_gaps: new code paths introduced with no visible unit test in the diff
- tech_debt_introduced: shortcuts taken that will need to be revisited (comment with reason if you can infer it)

{team_style_addendum}

Return ONLY valid JSON, no prose. Exact structure:
{{
  "findings": [
    {{
      "type": "<issue class>",
      "severity": "MEDIUM|LOW",
      "rule_ref": "<PEP8/mypy/ruff rule ID>",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong and why it matters>",
      "maintainability_score_impact": "HIGH|MEDIUM|LOW",
      "fix": "<exact correction — show the fixed code snippet if < 3 lines>"
    }}
  ],
  "gap_analysis": {{
    "maintainability_risks": ["<pattern that causes breakage or confusion at scale>"],
    "missing_contracts": ["<public function/class lacking type annotation>"],
    "test_coverage_gaps": ["<new code path with no unit test in diff>"],
    "tech_debt_introduced": ["<shortcut taken that needs future cleanup>"]
  }},
  "ac_style_verdict": {{
    "status": "PASS|FAIL",
    "evaluated_items": [
      {{
        "criterion": "<criterion text>",
        "status": "PASS|FAIL|PARTIAL|UNVERIFIABLE",
        "evidence": "<specific diff evidence>"
      }}
    ]
  }}
}}

If no findings exist return empty array for findings and PASS for ac_style_verdict. Still populate gap_analysis.
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
    if "gap_analysis" not in parsed:
        parsed["gap_analysis"] = {
            "maintainability_risks": [],
            "missing_contracts": [],
            "test_coverage_gaps": [],
            "tech_debt_introduced": [],
        }
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


from bedrock_agentcore.runtime import BedrockAgentCoreApp

_app = BedrockAgentCoreApp()


@_app.entrypoint
async def handler(payload: dict) -> dict:
    pr_diff = payload.get("pr_diff", "")
    ac_style_items = payload.get("ac_style_items", [])
    return run_style_enforcement(pr_diff, ac_style_items)


if __name__ == "__main__":
    _app.run()
