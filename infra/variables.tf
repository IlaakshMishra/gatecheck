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
  default = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}
