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

output "ecr_urls" {
  value = { for k, v in aws_ecr_repository.agents : k => v.repository_url }
}
