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

## Key implementation notes

- All 4 sub-agents invoked concurrently via `asyncio.gather` + `asyncio.to_thread` (real parallelism — boto3 is sync)
- Session IDs padded to ≥ 33 chars (AgentCore requirement)
- Sub-agent A2A servers: tries `bedrock_agentcore.runtime.serve_a2a` first, falls back to `A2AStarletteApplication` + uvicorn
- Untrusted PR content (body, title) passed through env vars in GitHub Actions — never inline `${{ }}` in `run:` (shell injection prevention)
- OIDC role assumption — no long-lived AWS keys stored in GitHub secrets
- `python:3.13-slim` base image — NOT the Lambda base (Lambda ENTRYPOINT conflicts with long-running server)
- StyleEnforcerAgent reads team coding standards from AgentCore Memory when `MEMORY_ID` is set
- Terraform pins `hashicorp/aws ~> 6.32` — run `terraform plan` before apply to catch provider schema changes
