resource "aws_bedrockagentcore_gateway" "github_gateway" {
  name        = "${var.project}-github-gateway"
  description = "GitHub API access for PR diff fetch and comment posting"

  execution_role_arn = aws_iam_role.agent_execution_role.arn

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = null
      allowed_audience = []
      allowed_clients  = []
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
        # Known gap: grantType CLIENT_CREDENTIALS silently dropped in ~> 6.32.
        # If gateway auth fails, call GitHub API directly from orchestrator code
        # using the secret ARN — same security posture, less ceremony.
        api_key_credential_provider {
          credential_parameter_name = "Authorization"
          secret_arn                = aws_secretsmanager_secret.github_token.arn
        }
      }
    }
  }
}
