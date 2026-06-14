resource "aws_bedrockagentcore_memory" "shared" {
  name        = "${var.project}-shared-memory"
  description = "Team coding standards, AC patterns, review history"

  memory_execution_role_arn = aws_iam_role.agent_execution_role.arn

  event_expiry_duration = 7
}
