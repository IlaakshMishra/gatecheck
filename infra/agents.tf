resource "aws_bedrockagentcore_agent_runtime" "orchestrator" {
  agent_runtime_name = "${replace(var.project, "-", "_")}_orchestrator"
  description        = "Coordinator: parses AC, fans out to sub-agents via A2A, synthesizes verdict"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["orchestrator"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID             = var.model_id
    MEMORY_ID            = aws_bedrockagentcore_memory.shared.id
    GITHUB_SECRET_ARN    = aws_secretsmanager_secret.github_token.arn
    AC_VERIFIER_ARN      = aws_bedrockagentcore_agent_runtime.ac_verifier.agent_runtime_arn
    SECURITY_AUDITOR_ARN = aws_bedrockagentcore_agent_runtime.security_auditor.agent_runtime_arn
    PERF_ANALYZER_ARN    = aws_bedrockagentcore_agent_runtime.perf_analyzer.agent_runtime_arn
    STYLE_ENFORCER_ARN   = aws_bedrockagentcore_agent_runtime.style_enforcer.agent_runtime_arn
  }

  depends_on = [
    aws_bedrockagentcore_agent_runtime.ac_verifier,
    aws_bedrockagentcore_agent_runtime.security_auditor,
    aws_bedrockagentcore_agent_runtime.perf_analyzer,
    aws_bedrockagentcore_agent_runtime.style_enforcer,
  ]
}

resource "aws_bedrockagentcore_agent_runtime" "ac_verifier" {
  agent_runtime_name = "${replace(var.project, "-", "_")}_ac_verifier"
  description        = "Maps each AC item to diff lines, returns PASS/FAIL/PARTIAL/UNVERIFIABLE"

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agents["ac-verifier"].repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

resource "aws_bedrockagentcore_agent_runtime" "security_auditor" {
  agent_runtime_name = "${replace(var.project, "-", "_")}_security_auditor"
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

  role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

resource "aws_bedrockagentcore_agent_runtime" "perf_analyzer" {
  agent_runtime_name = "${replace(var.project, "-", "_")}_perf_analyzer"
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

  role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID = var.model_id
  }
}

resource "aws_bedrockagentcore_agent_runtime" "style_enforcer" {
  agent_runtime_name = "${replace(var.project, "-", "_")}_style_enforcer"
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

  role_arn = aws_iam_role.agent_execution_role.arn

  environment_variables = {
    MODEL_ID  = var.model_id
    MEMORY_ID = aws_bedrockagentcore_memory.shared.id
  }
}
