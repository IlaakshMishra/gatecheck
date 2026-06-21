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

PERF_SYSTEM_PROMPT = """You are an expert performance engineer (SRE / staff-level) specializing in backend code review.

Analyze the provided PR diff for ALL of the following performance issue classes:
1. N+1 query problems — ORM queries inside loops, lazy loading triggered per iteration; estimate how many queries per request
2. Blocking I/O in async paths — synchronous DB calls, file I/O, or HTTP requests inside async functions without await or thread offloading; identify event loop stall duration
3. Unbounded loops or recursion — loops over collections with no size cap, unbounded pagination, missing LIMIT clauses; state what happens at 10k / 1M records
4. Big-O regressions — algorithmic complexity increases due to nested loops, cross-joins, or redundant re-computation; state before/after complexity
5. Missing database indices — queries filtering or sorting on columns likely lacking an index; estimate full-table-scan cost at scale
6. Memory leaks — objects appended to module-level state, unclosed file handles, event listeners never removed, growing caches with no eviction
7. Chatty external calls — multiple sequential HTTP/RPC calls that could be batched or parallelised
8. Cold-start cost — large imports, expensive initialisation at module level that runs on every Lambda/container cold start
9. Response time acceptance criteria thresholds — any AC item specifying latency/throughput SLAs; verdict if diff makes them achievable or not

For each finding, provide:
- complexity_impact: the algorithmic change (e.g. "O(1) → O(n)" or "1 query → n queries per request")
- scale_threshold: the approximate data size or concurrency level where this becomes a production incident
- estimated_latency_impact: rough estimate of added latency (e.g. "+50ms at 1k rows", "unbounded at scale")

After findings, produce gap_analysis:
- scalability_risks: patterns that work today but will fail at 10x / 100x load
- unmeasured_slas: performance SLAs mentioned in AC that are unverifiable from the diff alone
- future_bottlenecks: new code paths that will become hotspots as the feature grows
- missing_observability: missing metrics, traces, or profiling hooks that would catch regressions in production

Return ONLY valid JSON, no prose. Exact structure:
{
  "findings": [
    {
      "type": "<issue class>",
      "severity": "HIGH|MEDIUM|LOW",
      "line": "<file:line or hunk reference>",
      "description": "<what is wrong and its performance impact — be specific>",
      "complexity_impact": "<before/after Big-O or query count>",
      "scale_threshold": "<data size or concurrency where this causes a production incident>",
      "estimated_latency_impact": "<rough latency cost estimate>",
      "fix": "<concrete step-by-step optimization>"
    }
  ],
  "gap_analysis": {
    "scalability_risks": ["<pattern that fails at 10x/100x load>"],
    "unmeasured_slas": ["<SLA from AC that cannot be verified from diff>"],
    "future_bottlenecks": ["<new code path that will become a hotspot>"],
    "missing_observability": ["<missing metric/trace/profiling hook>"]
  },
  "ac_perf_verdict": {
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

If no findings exist return empty array for findings and PASS for ac_perf_verdict. Still populate gap_analysis.
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
    if "gap_analysis" not in parsed:
        parsed["gap_analysis"] = {
            "scalability_risks": [],
            "unmeasured_slas": [],
            "future_bottlenecks": [],
            "missing_observability": [],
        }
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


from bedrock_agentcore.runtime import BedrockAgentCoreApp

_app = BedrockAgentCoreApp()


@_app.entrypoint
async def handler(payload: dict) -> dict:
    pr_diff = payload.get("pr_diff", "")
    ac_perf_items = payload.get("ac_perf_items", [])
    return run_perf_analysis(pr_diff, ac_perf_items)


if __name__ == "__main__":
    _app.run()
