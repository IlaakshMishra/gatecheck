# AC-Driven PR Review System — Execution Plan
### Stack: Bedrock AgentCore CLI · LangGraph · A2A Protocol · Terraform · Claude Sonnet 4.6

---

## ⚠️ Critical Corrections (read before building)

This plan has real gaps that break at deploy/runtime. Fixes below are folded into
the phases, but the architecture-level ones are collected here.

### C1 — Model: Sonnet 4 is retiring (2026-06-15)

`us.anthropic.claude-sonnet-4-20250514` is the deprecated **Claude Sonnet 4**,
retired June 15, 2026. Use the current Sonnet cross-region inference profile:

```
us.anthropic.claude-sonnet-4-6     # Claude Sonnet 4.6
us.anthropic.claude-opus-4-8       # Opus 4.8 — if you want max capability
```

### C2 — Declare the server protocol in Terraform

`aws_bedrockagentcore_agent_runtime` defaults to the **HTTP** contract (container
must listen on port **8080** at `/invocations` + `/ping`). The sub-agents in this
plan serve **A2A on port 9000** — without declaring it, the runtime fails its
health check and never reaches ACTIVE. Add to every A2A sub-agent resource:

```hcl
  protocol_configuration {
    server_protocol = "A2A"
  }
```

The orchestrator stays HTTP (default), so it gets no protocol block.

### C3 — Orchestrator ↔ sub-agent protocol mismatch (pick one)

The orchestrator calls sub-agents with `invoke_agent_runtime(payload=json.dumps(...))`
— raw JSON. But an A2A server expects a JSON-RPC envelope. These don't match.
Two clean options:

- **Recommended (simpler):** make sub-agents plain `BedrockAgentCoreApp` HTTP
  entrypoints (port 8080, no A2A). Then `invoke_agent_runtime` with raw JSON works
  as written and you delete all A2A wrapper code + protocol blocks.
- **Keep A2A:** the orchestrator must speak A2A — use an A2A client against each
  sub-agent ARN (not raw `invoke_agent_runtime`). More moving parts.

The rest of this plan assumes you keep A2A; if you choose HTTP, drop §2.6 and C2.

### C4 — Corrected orchestrator skeleton (replaces the §2.1 stub)

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
import asyncio, boto3, json, os

app = BedrockAgentCoreApp()

SUB_AGENT_ARNS = {
    "ac_verifier":      os.environ["AC_VERIFIER_ARN"],
    "security_auditor": os.environ["SECURITY_AUDITOR_ARN"],
    "perf_analyzer":    os.environ["PERF_ANALYZER_ARN"],
    "style_enforcer":   os.environ["STYLE_ENFORCER_ARN"],
}

# Data-plane service is "bedrock-agentcore" (NOT "bedrock-agentcore-runtime")
client = boto3.client("bedrock-agentcore", region_name="us-west-2")

def _invoke_blocking(arn, payload, session_id):
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        payload=json.dumps(payload).encode("utf-8"),
        runtimeSessionId=session_id,   # MUST be >= 33 chars
    )
    # Non-streaming body comes back as a StreamingBody under "response"
    return json.loads(resp["response"].read())

async def invoke_sub_agent(name, arn, payload, session_id):
    try:
        # boto3 is sync — offload so the 4 calls actually run concurrently
        result = await asyncio.to_thread(_invoke_blocking, arn, payload, session_id)
        return name, {"ok": True, "result": result}
    except Exception as e:
        return name, {"ok": False, "error": str(e)}

async def fan_out(payload, session_id):
    tasks = [
        invoke_sub_agent(name, arn, payload, session_id)
        for name, arn in SUB_AGENT_ARNS.items()
    ]
    return dict(await asyncio.gather(*tasks))

@app.entrypoint
async def handler(payload):
    # session id must be >= 33 chars — pad/uuid if needed
    session_id = (payload.get("session_id") or "pr-review-session-0000000000000")[:128]
    if len(session_id) < 33:
        session_id = (session_id + "0" * 33)[:33]
    reports = await fan_out(payload, session_id)
    verdict = synthesize(reports)            # your synthesis logic
    post_to_github(payload, verdict)         # GitHub REST via PAT from Secrets Manager
    return {"verdict": verdict, "reports": reports}

if __name__ == "__main__":
    app.run()
```

### C5 — Toolkit install is pip, not npm

`npm install -g @aws/agentcore` does not exist. The AgentCore starter toolkit is a
Python package; the CLI verbs are `configure` / `launch` / `invoke` / `status`
(there is no `agentcore create` / `add agent` / `dev` / `traces`):

```bash
pip install bedrock-agentcore-starter-toolkit
agentcore --help
```

### C6 — IAM additions (see §4.4)

Add: `ecr:GetAuthorizationToken` (Resource `"*"`), Bedrock invoke on **both**
`inference-profile/*` and `foundation-model/*` (cross-region profiles span
regions), and Memory actions (`bedrock-agentcore:CreateEvent`, `ListEvents`,
`RetrieveMemoryRecords`).

---

## Prerequisites

Before touching any code, verify all of these:

```bash
node --version        # 20+
python --version      # 3.10+
aws --version         # CLI v2
terraform --version   # 1.6+ (use tfenv, not brew — brew gives 1.5.7)
docker --version      # needed for ARM64 container builds
```

Enable model access in the AWS Console:
- Go to: Amazon Bedrock → Model access → Request access
- Enable: `Anthropic Claude Sonnet 4.6` (us-west-2 region) — Sonnet 4.0 is deprecated (retires 2026-06-15)

Configure AWS credentials:
```bash
aws configure
# AWS Access Key ID: <your key>
# AWS Secret Access Key: <your secret>
# Default region: us-west-2
# Output format: json
```

---

## Phase 1 — Install & Scaffold Agent Code

### 1.1 Install the AgentCore CLI (for local dev only)

```bash
pip install bedrock-agentcore-starter-toolkit
agentcore --version
```

> Note: `npm install -g @aws/agentcore` does not exist. The toolkit is a Python
> package. CLI verbs: `configure` / `launch` / `invoke` / `status`. See §C5.

You still use the CLI for local dev and hot-reload testing. Terraform handles
all AWS provisioning — the CLI's `agentcore deploy` command is not used here.

### 1.2 Scaffold the project

```bash
agentcore create
```

Interactive wizard:
- **Project name:** `pr-review-system`
- **Framework:** `LangGraph`
- **Model provider:** `Amazon Bedrock`
- **Agent name:** `OrchestratorAgent`

```bash
cd pr-review-system
```

### 1.3 Add sub-agents (code scaffold only, not deploying yet)

```bash
agentcore add agent   # ACVerifierAgent    — LangGraph — byo
agentcore add agent   # SecurityAuditorAgent — LangGraph — byo
agentcore add agent   # PerfAnalyzerAgent  — LangGraph — byo
agentcore add agent   # StyleEnforcerAgent — LangGraph — byo
```

This generates the `app/<AgentName>/main.py` stubs you'll write into.
The `agentcore/cdk/` folder that CLI generates — ignore it. Terraform owns infra.

---

## Phase 2 — Write the Agent Code

Each agent lives in `app/<AgentName>/main.py`.

### 2.1 OrchestratorAgent

Responsibilities:
1. Accept: `{ pr_diff, acceptance_criteria, pr_description, repo_url, pr_number }`
2. Parse AC into structured list
3. Fan out to 4 sub-agents via A2A in parallel
4. Collect reports, synthesize verdict
5. Post formatted markdown table to GitHub PR

```python
from langchain_aws import ChatBedrockConverse
from langgraph.graph import StateGraph
from bedrock_agentcore.runtime import BedrockAgentCoreApp
import asyncio, boto3, json, os

# Sub-agent ARNs injected by Terraform as env vars
SUB_AGENT_ARNS = {
    "ac_verifier":      os.environ["AC_VERIFIER_ARN"],
    "security_auditor": os.environ["SECURITY_AUDITOR_ARN"],
    "perf_analyzer":    os.environ["PERF_ANALYZER_ARN"],
    "style_enforcer":   os.environ["STYLE_ENFORCER_ARN"],
}

# NOTE: this stub is buggy — see §C4 for the corrected, complete version.
# Bugs: wrong service name, wrong response key, sync boto3 inside asyncio
# (no real parallelism), and no app entrypoint / GitHub post.
client = boto3.client("bedrock-agentcore", region_name="us-west-2")  # not "-runtime"

def _invoke_blocking(arn, payload, session_id):
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        payload=json.dumps(payload).encode("utf-8"),
        runtimeSessionId=session_id,        # >= 33 chars
    )
    return json.loads(resp["response"].read())   # StreamingBody under "response"

async def invoke_sub_agent(name, arn, payload, session_id):
    result = await asyncio.to_thread(_invoke_blocking, arn, payload, session_id)
    return name, result

async def fan_out(payload, session_id):
    tasks = [
        invoke_sub_agent(name, arn, payload, session_id)
        for name, arn in SUB_AGENT_ARNS.items()
    ]
    return dict(await asyncio.gather(*tasks))
```

### 2.2 ACVerifierAgent

Receives: `{ pr_diff, acceptance_criteria_list }`

Returns: `[ { criterion, status: "PASS"|"FAIL"|"PARTIAL"|"UNVERIFIABLE", evidence, line_refs } ]`

System prompt:
```
You are an Acceptance Criteria Verifier. You receive a PR diff and a list of
acceptance criteria. For EACH criterion:
1. Find which lines in the diff are relevant
2. Determine if the implementation satisfies it fully, partially, or not at all
3. Return structured JSON only — no prose

Be strict. PARTIAL is not a PASS. If you cannot verify from the diff alone,
return UNVERIFIABLE with a reason. Never guess.
```

### 2.3 SecurityAuditorAgent

Receives: `{ pr_diff, ac_security_items }`
Checks: OWASP Top 10, hardcoded secrets, IAM escalation, injection vectors, AC security requirements
Returns: `{ findings: [...], ac_security_verdict: {...} }`

### 2.4 PerfAnalyzerAgent

Receives: `{ pr_diff, ac_perf_items }`
Checks: N+1 queries, blocking calls in async, Big-O regressions, AC perf thresholds
Returns: `{ findings: [...], ac_perf_verdict: {...} }`

### 2.5 StyleEnforcerAgent

Receives: `{ pr_diff, ac_style_items }`
Checks: naming conventions, dead code, missing docstrings, import hygiene
Returns: `{ findings: [...], ac_style_verdict: {...} }`

### 2.6 A2A wrapper for each sub-agent

Every sub-agent needs this wrapper to run on port 9000:

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TextPart
from bedrock_agentcore.runtime import serve_a2a

class ReviewAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        result = await graph.ainvoke({"messages": [("user", context.get_user_input())]})
        updater.add_artifact([TextPart(text=result["messages"][-1].content)])
        updater.complete()

    async def cancel(self, context, event_queue):
        raise NotImplementedError

agent_card = AgentCard(
    name="ACVerifierAgent",
    description="Validates PR diff against acceptance criteria",
    url="http://localhost:9000/",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=False),
    skills=[AgentSkill(id="ac_verify", name="AC Verification", description="Maps AC to code")]
)

if __name__ == "__main__":
    serve_a2a(ReviewAgentExecutor(), agent_card=agent_card)
```

A2A servers must run on port 9000 at path `/`. Agent card auto-served at
`/.well-known/agent-card.json`.

> ⚠️ Verify `serve_a2a` exists in your installed `bedrock-agentcore` version —
> A2A serving is often done via the `a2a` library's Starlette app, not a
> `bedrock_agentcore.runtime.serve_a2a` helper. If absent, wrap with
> `A2AStarletteApplication` and run uvicorn on `0.0.0.0:9000`.
>
> Also add real error handling / per-call timeouts in `fan_out` — the corrected
> version (§C4) catches per-agent so one failing sub-agent doesn't kill `gather`.

---

## Phase 3 — Build & Push Docker Images to ECR

AgentCore Runtime requires ARM64 containers. This is the step most tutorials
skip and then wonder why their containers crash.

Each agent gets its own ECR repo and image.

```bash
# Login to ECR
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin \
    <your_account_id>.dkr.ecr.us-west-2.amazonaws.com

# Build and push each agent — must target linux/arm64
for agent in OrchestratorAgent ACVerifierAgent SecurityAuditorAgent PerfAnalyzerAgent StyleEnforcerAgent; do
  docker buildx build \
    --platform linux/arm64 \
    -t <your_account_id>.dkr.ecr.us-west-2.amazonaws.com/pr-review-${agent,,}:latest \
    --push \
    ./app/$agent
done
```

Each agent needs a `Dockerfile`:

```dockerfile
# Use a plain Python base — NOT the Lambda base image. The Lambda image ships a
# Runtime Interface Client as ENTRYPOINT that fights a long-running server CMD.
FROM --platform=linux/arm64 python:3.13-slim
WORKDIR /app
# Copy source BEFORE install so `pip install -e .` has a package to build.
COPY pyproject.toml main.py ./
RUN pip install --no-cache-dir -e .
# 9000 for A2A sub-agents. Orchestrator (HTTP contract) must EXPOSE 8080.
EXPOSE 9000
CMD ["python", "main.py"]
```

Each agent's `pyproject.toml` must declare deps, e.g.:

```toml
[project]
name = "pr-review-agent"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
    "bedrock-agentcore",
    "langgraph",
    "langchain-aws",
    "a2a-sdk",        # only for A2A sub-agents
    "boto3",
]
```

ECR repos are created by Terraform in Phase 4 — run Terraform first, then
build and push images.

> ⚠️ The loop above tags `pr-review-${agent,,}` → `pr-review-orchestratoragent`,
> but Terraform (`local.agents`) creates `pr-review-orchestrator`,
> `pr-review-ac-verifier`, etc. — names DON'T match. Use the `ecr_urls` Terraform
> output to get the real repo URLs (as Phase 5 does) instead of constructing them.

---

## Phase 4 — Terraform Infrastructure

This is the core of the change. One `terraform apply` provisions everything.
One `terraform destroy` tears it all down cleanly after testing.

### 4.1 Project structure

```
pr-review-system/
├── app/                          # agent code (Phases 1–2)
├── infra/                        # all Terraform lives here
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── iam.tf
│   ├── ecr.tf
│   ├── agents.tf
│   ├── memory.tf
│   ├── gateway.tf
│   └── secrets.tf
├── .github/
│   └── workflows/
│       └── pr-review.yml
└── README.md
```

### 4.2 `infra/variables.tf`

```hcl
variable "aws_region" {
  default = "us-west-2"
}

variable "project" {
  default = "pr-review"
}

variable "github_token" {
  description = "GitHub PAT with repo + pull_requests scope"
  sensitive   = true
}

variable "model_id" {
  # Sonnet 4 (claude-sonnet-4-20250514) is DEPRECATED, retires 2026-06-15.
  # Current Sonnet cross-region inference profile:
  default = "us.anthropic.claude-sonnet-4-6"
  # Max capability instead: "us.anthropic.claude-opus-4-8"
}
```

### 4.3 `infra/main.tf`

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.32"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
```

### 4.4 `infra/iam.tf`

One execution role shared across all 5 agents. Scoped to Bedrock + ECR + CloudWatch + Secrets.

```hcl
data "aws_caller_identity" "current" {}

resource "aws_iam_role" "agent_execution_role" {
  name = "${var.project}-agent-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "agent_policy" {
  name = "${var.project}-agent-policy"
  role = aws_iam_role.agent_execution_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Cross-region inference profile invoke needs BOTH the profile ARN and
        # the underlying foundation models in every region the profile spans.
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = "*"
      },
      {
        # Agents use MEMORY_ID — without these the memory calls 403.
        Effect   = "Allow"
        Action   = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:RetrieveMemoryRecords"
        ]
        Resource = "*"
      },
      {
        # GetAuthorizationToken MUST be a separate statement on Resource "*".
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.github_token.arn
      }
    ]
  })
}
```

### 4.5 `infra/ecr.tf`

```hcl
locals {
  agents = ["orchestrator", "ac-verifier", "security-auditor", "perf-analyzer", "style-enforcer"]
}

resource "aws_ecr_repository" "agents" {
  for_each             = toset(local.agents)
  name                 = "${var.project}-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "ecr_urls" {
  value = { for k, v in aws_ecr_repository.agents : k => v.repository_url }
}
```

### 4.6 `infra/agents.tf`

```hcl
# Orchestrator
resource "aws_bedrockagentcore_agent_runtime" "orchestrator" {
  agent_runtime_name = "${var.project}-orchestrator"
  description        = "Coordinator: parses AC, fans out to sub-agents via A2A, synthesizes verdict"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["orchestrator"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID              = var.model_id
    MEMORY_ID             = aws_bedrockagentcore_memory.shared.id
    GITHUB_SECRET_ARN     = aws_secretsmanager_secret.github_token.arn
    # Sub-agent ARNs injected after they are created — use depends_on
    AC_VERIFIER_ARN       = aws_bedrockagentcore_agent_runtime.ac_verifier.agent_runtime_arn
    SECURITY_AUDITOR_ARN  = aws_bedrockagentcore_agent_runtime.security_auditor.agent_runtime_arn
    PERF_ANALYZER_ARN     = aws_bedrockagentcore_agent_runtime.perf_analyzer.agent_runtime_arn
    STYLE_ENFORCER_ARN    = aws_bedrockagentcore_agent_runtime.style_enforcer.agent_runtime_arn
  }

  depends_on = [
    aws_bedrockagentcore_agent_runtime.ac_verifier,
    aws_bedrockagentcore_agent_runtime.security_auditor,
    aws_bedrockagentcore_agent_runtime.perf_analyzer,
    aws_bedrockagentcore_agent_runtime.style_enforcer,
  ]
}

# AC Verifier
resource "aws_bedrockagentcore_agent_runtime" "ac_verifier" {
  agent_runtime_name = "${var.project}-ac-verifier"
  description        = "Maps each AC item to diff lines, returns PASS/FAIL/PARTIAL/UNVERIFIABLE"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["ac-verifier"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  # Required: this container serves A2A on port 9000, not the default HTTP/8080.
  protocol_configuration {
    server_protocol = "A2A"
  }

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

# Security Auditor
resource "aws_bedrockagentcore_agent_runtime" "security_auditor" {
  agent_runtime_name = "${var.project}-security-auditor"
  description        = "OWASP checks, secrets scanning, IAM analysis, AC security items"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["security-auditor"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

# Perf Analyzer
resource "aws_bedrockagentcore_agent_runtime" "perf_analyzer" {
  agent_runtime_name = "${var.project}-perf-analyzer"
  description        = "N+1 patterns, Big-O analysis, AC performance thresholds"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["perf-analyzer"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

# Style Enforcer
resource "aws_bedrockagentcore_agent_runtime" "style_enforcer" {
  agent_runtime_name = "${var.project}-style-enforcer"
  description        = "Naming, dead code, docstrings, team standards from memory"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["style-enforcer"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID  = var.model_id
    MEMORY_ID = aws_bedrockagentcore_memory.shared.id
  }
}
```

### 4.7 `infra/memory.tf`

```hcl
resource "aws_bedrockagentcore_memory" "shared" {
  name        = "${var.project}-shared-memory"
  description = "Team coding standards, AC patterns, review history"

  memory_execution_role_arn = aws_iam_role.agent_execution_role.arn

  event_expiry_duration = 7  # days — short-term events expire after 7 days
}
```

### 4.8 `infra/gateway.tf`

```hcl
resource "aws_bedrockagentcore_gateway" "github_gateway" {
  name        = "${var.project}-github-gateway"
  description = "GitHub API access for PR diff fetch and comment posting"

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  authorizer_configuration {
    # IAM auth — agents call gateway using SigV4
    custom_jwt_authorizer {
      discovery_url         = null
      allowed_audience      = []
      allowed_clients       = []
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "github_api" {
  name       = "github-api"
  gateway_id = aws_bedrockagentcore_gateway.github_gateway.gateway_id

  target_configuration {
    open_api_schema {
      uri         = "https://api.github.com"
      description = "GitHub REST API for PR operations"

      credential_provider {
        api_key_credential_provider {
          credential_parameter_name = "Authorization"
          secret_arn                = aws_secretsmanager_secret.github_token.arn
        }
      }
    }
  }
}
```

> ⚠️ Known gap: as of hashicorp/aws ~> 6.32, the `grantType:
> CLIENT_CREDENTIALS` field on credential configs is dropped silently.
> If Gateway auth fails, fall back to calling the GitHub API directly
> from the Orchestrator using the secret ARN — same security posture,
> less ceremony.

### 4.9 `infra/secrets.tf`

```hcl
resource "aws_secretsmanager_secret" "github_token" {
  name                    = "${var.project}/github-token"
  description             = "GitHub PAT for PR diff read and comment write"
  recovery_window_in_days = 0  # instant delete on terraform destroy
}

resource "aws_secretsmanager_secret_version" "github_token" {
  secret_id     = aws_secretsmanager_secret.github_token.id
  secret_string = var.github_token
}
```

### 4.10 `infra/outputs.tf`

```hcl
output "orchestrator_arn" {
  value       = aws_bedrockagentcore_agent_runtime.orchestrator.agent_runtime_arn
  description = "Use this ARN in GitHub Actions to invoke the review"
}

output "gateway_endpoint" {
  value = aws_bedrockagentcore_gateway.github_gateway.gateway_url
}

output "memory_id" {
  value = aws_bedrockagentcore_memory.shared.id
}

output "ecr_repository_urls" {
  value = { for k, v in aws_ecr_repository.agents : k => v.repository_url }
}
```

---

## Phase 5 — Deploy Order

Do this in order — skipping steps breaks dependencies.

```bash
cd infra

# 1. Init Terraform
terraform init

# 2. Preview what gets created
terraform plan -var="github_token=ghp_xxxx"

# 3. Create ECR repos and all AgentCore resources
terraform apply -var="github_token=ghp_xxxx"

# 4. Build and push ARM64 images AFTER ECR repos exist
cd ..
ECR_ORCHESTRATOR=$(terraform -chdir=infra output -raw ecr_urls | jq -r '.orchestrator')
# repeat for each agent using the ecr_urls output

docker buildx build --platform linux/arm64 \
  -t $ECR_ORCHESTRATOR:latest --push ./app/OrchestratorAgent
# ... repeat for other 4 agents

# 5. Verify all runtimes are active
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2
```

---

## Phase 6 — Local Testing (Before Any AWS Deploy)

Test each agent locally first. The AgentCore CLI's dev server handles this.

> ⚠️ Verb names below (`agentcore dev` / `invoke` / `logs` / `traces`) are
> illustrative — confirm against `agentcore --help` for your installed toolkit
> version (see §C5). Real verbs are `configure` / `launch` / `invoke` / `status`.
> For an A2A sub-agent, also confirm it binds `0.0.0.0:9000` locally.

```bash
# Start local dev server for the orchestrator
agentcore dev

# Test with a sample payload
agentcore invoke --payload '{
  "pr_diff": "diff --git a/app.py b/app.py\n+def login(user, pwd):\n+    return db.query(f\"SELECT * FROM users WHERE pwd={pwd}\")",
  "acceptance_criteria": "- User must be authenticated via JWT\n- No raw SQL queries in auth layer\n- Response under 200ms",
  "pr_description": "Adds login endpoint",
  "repo_url": "https://github.com/your-org/repo",
  "pr_number": 42
}'

# Test sub-agents on port 9000
cd app/ACVerifierAgent && python main.py &
curl -X POST http://localhost:9000 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"id":"t1","message":{"role":"user","parts":[{"type":"text","text":"verify AC"}]}}}'

# Verify agent card
curl http://localhost:9000/.well-known/agent-card.json
```

---

## Phase 7 — Post-Deploy Testing

```bash
# Invoke deployed orchestrator directly.
# Service is "bedrock-agentcore" (data plane). Payload is a blob (base64/fileb),
# and an OUTPUT FILE is a required positional arg. session-id must be >= 33 chars.
echo '{"pr_diff":"...","acceptance_criteria":"...","pr_description":"...","repo_url":"...","pr_number":1}' > /tmp/payload.json

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn $(terraform -chdir=infra output -raw orchestrator_arn) \
  --payload fileb:///tmp/payload.json \
  --runtime-session-id "pr-review-test-session-000000000000" \
  --region us-west-2 \
  /tmp/response.json

cat /tmp/response.json

# Stream logs
agentcore logs --since 30m

# View A2A traces (shows per-sub-agent timing)
agentcore traces list
agentcore traces get <trace-id>
```

---

## Phase 8 — GitHub Actions Integration

Add to your target repo at `.github/workflows/pr-review.yml`:

```yaml
name: AC PR Review
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      id-token: write          # for OIDC role assumption (no long-lived keys)
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # OIDC instead of static AWS_ACCESS_KEY_ID/SECRET secrets.
      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_REVIEW_ROLE_ARN }}
          aws-region: us-west-2

      - name: Get PR diff
        # Write to a file, not $GITHUB_OUTPUT — multiline diffs need the heredoc
        # delimiter form and large diffs blow GITHUB_OUTPUT limits.
        run: git diff "origin/${{ github.base_ref }}...HEAD" > /tmp/pr.diff

      - name: Trigger AgentCore Review
        env:
          AGENT_ARN: ${{ secrets.AGENT_ARN }}
          # Pass untrusted PR text through env, NEVER via ${{ }} interpolation
          # inside run: (shell-injection vector).
          PR_BODY: ${{ github.event.pull_request.body }}
          PR_TITLE: ${{ github.event.pull_request.title }}
          REPO_URL: ${{ github.event.repository.html_url }}
          PR_NUMBER: ${{ github.event.number }}
        run: |
          # session id must be >= 33 chars
          SID="pr-review-${{ github.event.number }}-${{ github.run_id }}-000000000000"
          SID="${SID:0:64}"

          jq -n \
            --rawfile diff /tmp/pr.diff \
            --arg ac "$PR_BODY" \
            --arg desc "$PR_TITLE" \
            --arg url "$REPO_URL" \
            --argjson num "$PR_NUMBER" \
            '{pr_diff: $diff, acceptance_criteria: $ac, pr_description: $desc, repo_url: $url, pr_number: $num}' \
            > /tmp/payload.json

          aws bedrock-agentcore invoke-agent-runtime \
            --agent-runtime-arn "$AGENT_ARN" \
            --runtime-session-id "$SID" \
            --payload fileb:///tmp/payload.json \
            --region us-west-2 \
            /tmp/response.json
```

The orchestrator posts the review comment directly to GitHub via the GitHub API
using the PAT stored in Secrets Manager.

---

## Phase 9 — Teardown

When you're done testing, destroy everything with one command:

```bash
cd infra
terraform destroy -var="github_token=ghp_xxxx"
```

This removes: 5 AgentCore runtimes, 5 ECR repos, 1 Gateway, 1 Gateway target,
1 Memory resource, 1 Secrets Manager secret, IAM role + policy.

The `recovery_window_in_days = 0` on the secret ensures it's gone immediately
rather than sitting in a 7-day recovery window.

> ⚠️ Known destroy issue: `aws_bedrockagentcore_agent_runtime` can leave behind
> network interfaces that create a circular dependency and cause destroy to hang.
> If this happens: go to EC2 → Network Interfaces in the console, manually
> detach and delete any interfaces tagged with your project name, then re-run
> `terraform destroy`.

---

## Phase 10 — Portfolio Hardening

### README must include

- Architecture diagram (the HTML file generated earlier)
- A real PR example with the GitHub comment screenshot
- `terraform apply` → `terraform destroy` workflow highlighted — shows you
  know IaC, not just vibe-coding agents
- Metrics: avg review time, token cost per review, AC coverage %

### Record a demo video showing

1. A real PR with 3+ AC items opened on a public repo
2. GitHub Action triggering the review automatically
3. The structured pass/fail comment appearing on the PR
4. CloudWatch traces showing all 4 sub-agents firing in parallel

---

## Final Project Structure

```
pr-review-system/
├── app/
│   ├── OrchestratorAgent/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── ACVerifierAgent/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── SecurityAuditorAgent/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── PerfAnalyzerAgent/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   └── StyleEnforcerAgent/
│       ├── main.py
│       ├── Dockerfile
│       └── pyproject.toml
├── infra/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── iam.tf
│   ├── ecr.tf
│   ├── agents.tf
│   ├── memory.tf
│   ├── gateway.tf
│   └── secrets.tf
├── .github/
│   └── workflows/
│       └── pr-review.yml
├── architecture.html
└── README.md
```

---

## Cheatsheet

```bash
# Terraform
terraform init                              # init providers
terraform plan  -var="github_token=..."    # dry run
terraform apply -var="github_token=..."    # provision everything
terraform destroy -var="github_token=..."  # tear down everything

# Docker (run after terraform apply creates ECR repos)
docker buildx build --platform linux/arm64 -t <ecr_url>:latest --push ./app/<AgentName>

# Local dev
agentcore dev                              # hot-reload local server
agentcore invoke --payload '{...}'         # test locally

# Post-deploy
agentcore logs --since 30m
agentcore traces list
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2
```

---

## Known Terraform Provider Gaps (as of hashicorp/aws ~> 6.32)

These are real gaps — not bugs in your code.

| Gap | Workaround |
|-----|-----------|
| `aws_bedrockagentcore_policy_engine` resource does not exist yet | Use `null_resource` + `aws_cli` provider or skip Policy for v1 |
| Gateway credential `grantType: CLIENT_CREDENTIALS` silently dropped | Call GitHub API directly from agent code using Secrets Manager ARN |
| Runtime destroy can hang on leftover network interfaces | Manually delete ENIs in EC2 console, then re-run destroy |
| Memory `SEMANTIC` strategy not yet in Terraform | Use `aws_bedrockagentcore_memory` without strategy block — defaults to SUMMARIZATION |
| A2A runtimes need `protocol_configuration { server_protocol = "A2A" }` or health checks fail | Added in §4.6 — see §C2 |
| Resource/arg names (`agent_runtime_artifact`, `memory_execution_role_arn`, `protocol_configuration`) may drift between provider minor versions | `terraform plan` and read provider docs before apply; pin the exact provider version |

---

## Estimated Build Time

| Phase | Effort |
|-------|--------|
| Scaffold + agent code | 2–3 days |
| Dockerfiles + ARM64 builds | 2–4 hours |
| Terraform infra | 3–4 hours |
| Local testing | half a day |
| Deploy + debug | 2–4 hours |
| GitHub Actions + portfolio | 1 day |
| **Total** | **~5–6 days focused** |

The ACVerifierAgent system prompt is where 80% of the quality comes from.
Spend disproportionate time on it. The Terraform is the easy part.
