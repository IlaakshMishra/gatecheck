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
