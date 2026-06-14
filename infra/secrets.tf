resource "aws_secretsmanager_secret" "github_token" {
  name                    = "${var.project}/github-token"
  description             = "GitHub PAT for PR diff read and comment write"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "github_token" {
  secret_id     = aws_secretsmanager_secret.github_token.id
  secret_string = var.github_token
}
