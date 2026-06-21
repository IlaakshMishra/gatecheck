# Gatecheck

AI-powered PR review system on AWS Bedrock AgentCore. Evaluates every pull request against its acceptance criteria using 5 specialized agents running in parallel, then posts a structured pass/fail verdict directly to the GitHub PR.

## Architecture

```
GitHub PR opened/updated
         │
         ▼
  GitHub Actions (OIDC)
         │
         ▼
┌─────────────────────────────┐
│      OrchestratorAgent      │  BedrockAgentCoreApp (HTTP/8080)
│                             │  LangGraph: parse_ac → categorize
│  Fan-out via A2A (parallel) │            → fan_out → synthesize
└──┬────┬────┬────────────┬───┘
   │    │    │            │
   ▼    ▼    ▼            ▼
 AC   Sec  Perf        Style
 Ver  Aud  Anal        Enforcer
 ifier itor yzer       Agent
                        (port 9000, A2A)
         │
         ▼
  GitHub PR comment (markdown table)
```

**Stack:** AWS Bedrock AgentCore · Claude Sonnet 4.6 · LangGraph · A2A Protocol · Terraform · GitHub Actions

## What each agent does

| Agent | Input | Output |
|---|---|---|
| **OrchestratorAgent** | PR diff + acceptance criteria + metadata | Fan-out coordinator, final verdict, GitHub comment |
| **ACVerifierAgent** | PR diff + AC list | PASS/FAIL/PARTIAL/UNVERIFIABLE per criterion with line refs |
| **SecurityAuditorAgent** | PR diff + security AC items | OWASP Top 10, hardcoded secrets, IAM, SQLi, XSS, command injection, path traversal |
| **PerfAnalyzerAgent** | PR diff + perf AC items | N+1 queries, blocking I/O in async, Big-O regressions, missing indices |
| **StyleEnforcerAgent** | PR diff + style AC items | Naming, dead code, type hints, docstrings, imports; reads team standards from AgentCore Memory |

## Quickstart

### Prerequisites

```bash
python --version    # 3.10+
aws --version       # CLI v2
terraform --version # 1.6+
docker --version    # for ARM64 builds
```

Enable model access in AWS Console → Bedrock → Model access:
- **Anthropic Claude Sonnet 4.6** (us-west-2)

### 1. Provision infrastructure

```bash
cd infra
terraform init
terraform plan  -var="github_token=ghp_xxxx"
terraform apply -var="github_token=ghp_xxxx"
```

Creates: 5 ECR repos, 5 AgentCore runtimes, IAM role, Secrets Manager secret, AgentCore Memory, Gateway.

### 2. Build and push ARM64 images

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin \
    $ACCOUNT.dkr.ecr.us-west-2.amazonaws.com

# Use Terraform output for correct repo URLs
ECR=$(terraform -chdir=infra output -json ecr_urls)

for agent in OrchestratorAgent ACVerifierAgent SecurityAuditorAgent PerfAnalyzerAgent StyleEnforcerAgent; do
  KEY=$(echo $agent | sed 's/Agent//' | sed 's/\([A-Z]\)/-\1/g' | tr '[:upper:]' '[:lower:]' | sed 's/^-//')
  URL=$(echo $ECR | jq -r ".[\"$KEY\"]")
  docker buildx build --platform linux/arm64 -t $URL:latest --push app/$agent
done
```

### 3. Verify runtimes are ACTIVE

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2
```

### 4. Wire up GitHub Actions

Add these secrets to your target repo:
- `AWS_REVIEW_ROLE_ARN` — IAM role ARN with permission to invoke the orchestrator (OIDC)
- `AGENT_ARN` — orchestrator ARN from `terraform output orchestrator_arn`

The workflow at `.github/workflows/pr-review.yml` triggers automatically on PR open/sync.

### 5. Test manually

```bash
echo '{"pr_diff":"...","acceptance_criteria":"- Users must auth via JWT\n- No raw SQL","pr_description":"Add login","repo_url":"https://github.com/org/repo","pr_number":1}' > /tmp/payload.json

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn $(terraform -chdir=infra output -raw orchestrator_arn) \
  --payload fileb:///tmp/payload.json \
  --runtime-session-id "gatecheck-manual-test-session-0000000000" \
  --region us-west-2 \
  /tmp/response.json

cat /tmp/response.json
```

### Teardown

```bash
terraform -chdir=infra destroy -var="github_token=ghp_xxxx"
```

Removes all AWS resources. The Secrets Manager secret is deleted immediately (`recovery_window_in_days = 0`).

## Project structure

```
gatecheck/
├── app/
│   ├── OrchestratorAgent/    main.py  Dockerfile  pyproject.toml
│   ├── ACVerifierAgent/      main.py  Dockerfile  pyproject.toml
│   ├── SecurityAuditorAgent/ main.py  Dockerfile  pyproject.toml
│   ├── PerfAnalyzerAgent/    main.py  Dockerfile  pyproject.toml
│   └── StyleEnforcerAgent/   main.py  Dockerfile  pyproject.toml
├── infra/
│   ├── main.tf         # provider + caller identity
│   ├── variables.tf    # region, project, model_id, github_token
│   ├── outputs.tf      # orchestrator_arn, ecr_urls, memory_id
│   ├── iam.tf          # shared execution role + 7-statement policy
│   ├── ecr.tf          # 5 repos via for_each
│   ├── agents.tf       # 5 AgentCore runtimes (4 A2A + 1 HTTP)
│   ├── memory.tf       # shared AgentCore Memory
│   ├── gateway.tf      # GitHub API gateway
│   └── secrets.tf      # GitHub PAT in Secrets Manager
├── .github/
│   └── workflows/pr-review.yml
└── README.md
```

## PR comment format

```markdown
## ❌ PR Review Verdict: FAIL

> Adds login endpoint

### Summary

| Category           | Count |
|--------------------|-------|
| Total Findings     | 3     |
| Security Findings  | 1     |
| Performance Findings | 0   |
| Style Findings     | 2     |
| AC Items Evaluated | 3     |

### Findings

| Category | Severity | Description                             | Fix / Evidence          |
|----------|----------|-----------------------------------------|-------------------------|
| SECURITY | CRITICAL | Raw SQL query with user input (SQLi)    | Use parameterized queries|
| AC       | HIGH     | JWT auth not implemented in diff        | auth.py:14-22           |
| STYLE    | LOW      | Missing type hint on `login` function   | Add `-> dict:`          |
```

## Demo branches

Two branches exist to show the system in action. Open PRs against `master` with the AC below to trigger the agent review.

### `demo/ac-pass` — what a PASSING review looks like

**PR description / acceptance criteria to paste:**

```
## What this PR does
Adds offset-based pagination utility for all list API endpoints.

## Acceptance Criteria
- Paginated responses must include total_count, total_pages, has_next, has_previous fields
- Page size must be capped at 100 items maximum
- Page numbers must be 1-based; page < 1 must raise ValueError
- All public functions must have type annotations and docstrings
- Unit tests must cover: first page, last page, partial last page, empty sequence, invalid page
- No raw SQL queries — pagination must be in-memory or use parameterized ORM filters
- No hardcoded credentials or secrets
```

The diff (`app/utils/pagination.py` + `test_pagination.py`) satisfies every criterion. Expected GateCheck result: **PASS** with no security or AC findings.

---

### `demo/ac-fail` — what a FAILING review looks like

**PR description / acceptance criteria to paste:**

```
## What this PR does
Adds user management service with database access and IAM role for the demo app.

## Acceptance Criteria
- All IAM policies must follow least-privilege principle — no wildcard Action ("*") or wildcard Resource ("*")
- No AWS managed AdministratorAccess policy may be attached to any IAM user or role
- No hardcoded credentials, API keys, passwords, or tokens in source code
- All database queries must use parameterized statements — string interpolation into SQL is prohibited
- All user-supplied values passed to subprocess calls must be validated against an allowlist
- All public functions must have type annotations and docstrings
- Sensitive operations (delete, admin creation) must include authorization checks before execution
- No secrets or passwords must appear in log output or stdout
```

The diff (`app/user_service.py` + `infra/demo_broken_iam.tf`) violates every criterion. Expected GateCheck result: **FAIL** with CRITICAL security findings across IAM, SQL injection, command injection, hardcoded credentials, and missing authorization.

---

## Plug GateCheck into any project

GateCheck is repository-agnostic. The orchestrator only needs a PR diff, acceptance criteria text, and GitHub metadata — it has no opinion on language or framework.

### Prerequisites

- AWS account with Bedrock access (us-west-2 or update `var.aws_region`)
- Bedrock model access enabled: **Claude Haiku 4.5** cross-region inference profile (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- Terraform 1.6+, Docker (ARM64 builds), AWS CLI v2
- GitHub repo with Actions enabled

### Step 1 — Clone and configure

```bash
git clone https://github.com/IlaakshMishra/gatecheck.git
cd gatecheck
```

Edit `infra/variables.tf` to set your AWS region and project name:

```hcl
variable "aws_region" { default = "us-west-2" }
variable "project"    { default = "your-project-name" }
```

Optionally swap the model by setting `TF_VAR_model_id` — any Bedrock cross-region inference profile works.

### Step 2 — Provision AWS infrastructure

```bash
cd infra
terraform init
terraform apply -var="github_token=ghp_YOUR_TOKEN"
```

This creates (all names prefixed with your project name):
- 5 ECR repositories
- 5 BedrockAgentCore runtimes (orchestrator + 4 sub-agents)
- IAM execution role with Bedrock, Secrets Manager, X-Ray, CloudWatch permissions
- GitHub OIDC provider + review role (no long-lived keys)
- Secrets Manager secret for the GitHub PAT
- AgentCore Memory for team style guide
- CloudWatch log groups, metric filters, alarms, dashboard
- AgentCore Gateway for GitHub API access

Note the outputs — you need `orchestrator_arn` and `github_actions_role_arn` for the next steps.

### Step 3 — Build and push container images

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin \
    $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

ECR=$(terraform output -json ecr_urls)

for agent in OrchestratorAgent ACVerifierAgent SecurityAuditorAgent PerfAnalyzerAgent StyleEnforcerAgent; do
  KEY=$(echo $agent | sed 's/Agent//' | python3 -c "
import sys, re
s = sys.stdin.read().strip()
s = re.sub(r'([A-Z])', r'-\1', s).lower().lstrip('-')
print(s)
")
  URL=$(echo $ECR | python3 -c "import sys,json; print(json.load(sys.stdin)['$KEY'])")
  docker buildx build --platform linux/arm64 -t $URL:latest --push app/$agent
done
```

Verify all 5 runtimes reach ACTIVE state:

```bash
aws bedrock-agentcore-control list-agent-runtimes --region $REGION \
  | python3 -c "import sys,json; [print(r['agentRuntimeName'], r['status']) for r in json.load(sys.stdin)['agentRuntimes']]"
```

### Step 4 — Copy the GitHub Actions workflow

Copy `.github/workflows/pr-review.yml` from this repo into your target repository (the repo whose PRs you want reviewed). No code changes needed.

```bash
cp -r .github/workflows /path/to/your-repo/.github/
```

### Step 5 — Set GitHub secrets on your target repository

Go to your repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Value |
|---|---|
| `AWS_REVIEW_ROLE_ARN` | Output of `terraform output -raw github_actions_role_arn` |
| `AGENT_ARN` | Output of `terraform output -raw orchestrator_arn` |

### Step 6 — Add AC to your PR descriptions

The orchestrator reads `acceptance_criteria` from the PR body. Use any format — bullet points, numbered list, prose. The AC Verifier agent parses it automatically.

Recommended PR template (save as `.github/pull_request_template.md` in your repo):

```markdown
## What this PR does
<!-- 1-3 sentence summary -->

## Acceptance Criteria
<!--
List every testable requirement this PR must satisfy.
GateCheck will evaluate each one against the diff.
Examples:
- Users can reset password via email link
- Password reset tokens expire after 15 minutes
- No raw SQL queries — use parameterized statements
- All new public functions have type hints and docstrings
- API endpoint returns 429 if rate limit exceeded
-->
```

### Step 7 — Open a PR and watch GateCheck review it

Open any PR on your target repo. Within ~60 seconds of opening or updating the PR, GateCheck posts a detailed review comment with:

- AC verdict per criterion with confidence scores and gap analysis
- Security findings with OWASP categories, attack vectors, and CVE examples
- Performance findings with Big-O impact and scale thresholds
- Style findings with rule references and maintainability impact
- Collapsible gap analysis sections for each agent

### Customising agent behaviour

| What to change | Where |
|---|---|
| Security checks (add/remove vuln classes) | `app/SecurityAuditorAgent/main.py` → `SECURITY_SYSTEM_PROMPT` |
| Performance thresholds | `app/PerfAnalyzerAgent/main.py` → `PERF_SYSTEM_PROMPT` |
| Style rules (e.g. add Go/TypeScript rules) | `app/StyleEnforcerAgent/main.py` → `BASE_STYLE_SYSTEM_PROMPT` |
| Team-specific style guide | Upload a `.md` file to AgentCore Memory; set `MEMORY_ID` env var |
| Model (cost vs quality trade-off) | `infra/variables.tf` → `model_id` or `TF_VAR_model_id` |
| Fail PR on style findings | `app/OrchestratorAgent/main.py` → `node_synthesize`, `style_findings` block |

After any agent code change, rebuild and push the affected image, then force-recreate the runtime:

```bash
docker buildx build --platform linux/arm64 -t $ECR_URL:latest --push app/SecurityAuditorAgent
terraform apply -replace=aws_bedrockagentcore_agent_runtime.security_auditor
```

### Cost estimate

At ~100 PRs/month with average 500-line diffs, running Claude Haiku 4.5:

| Component | Approx cost/month |
|---|---|
| Bedrock inference (5 agents × 100 PRs) | ~$8–15 |
| AgentCore runtime (5 runtimes, minimal idle) | ~$5–10 |
| ECR storage (5 images ~300MB each) | ~$1 |
| CloudWatch logs + metrics | ~$2 |
| **Total** | **~$16–28/month** |

Switch to Claude Sonnet 4.6 for deeper analysis at ~3–5× the inference cost.

---

## Key implementation notes

- All 4 sub-agents invoked concurrently via `asyncio.gather` + `asyncio.to_thread` (real parallelism — boto3 is sync)
- Security auditor always receives the full AC list regardless of category — security runs unconditionally
- Session IDs padded to ≥ 33 chars (AgentCore requirement)
- Sub-agent A2A servers: tries `bedrock_agentcore.runtime.serve_a2a` first, falls back to `A2AStarletteApplication` + uvicorn
- Untrusted PR content (body, title) passed through env vars in GitHub Actions — never inline `${{ }}` in `run:` (shell injection prevention)
- OIDC role assumption — no long-lived AWS keys stored in GitHub secrets
- `python:3.13-slim` base image — NOT the Lambda base (Lambda ENTRYPOINT conflicts with long-running server)
- StyleEnforcerAgent reads team coding standards from AgentCore Memory when `MEMORY_ID` is set
- Terraform pins `hashicorp/aws ~> 6.32` — run `terraform plan` before apply to catch provider schema changes
- `:latest` tag does NOT trigger image refresh on BedrockAgentCore; use `terraform apply -replace=aws_bedrockagentcore_agent_runtime.<name>` after pushing new images
