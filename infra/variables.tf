variable "aws_region" {
  default = "us-west-2"
}

variable "project" {
  default = "gatecheck"
}

variable "github_token" {
  description = "GitHub PAT with repo + pull_requests scope"
  sensitive   = true
}

variable "model_id" {
  # Sonnet 4 (claude-sonnet-4-20250514) is DEPRECATED, retires 2026-06-15.
  default = "us.anthropic.claude-sonnet-4-6"
}
