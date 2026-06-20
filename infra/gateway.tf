resource "aws_bedrockagentcore_api_key_credential_provider" "github" {
  name               = "${var.project}-github-token"
  api_key_wo         = var.github_token
  api_key_wo_version = 1
}

resource "aws_bedrockagentcore_gateway" "github_gateway" {
  name            = "${var.project}-github-gateway"
  description     = "GitHub API access for PR diff fetch and comment posting"
  role_arn        = aws_iam_role.agent_execution_role.arn
  authorizer_type = "NONE"
}

resource "aws_bedrockagentcore_gateway_target" "github_api" {
  name               = "github-api"
  gateway_identifier = aws_bedrockagentcore_gateway.github_gateway.gateway_id

  target_configuration {
    mcp {
      open_api_schema {
        inline_payload {
          payload = jsonencode({
            openapi = "3.0.0"
            info = {
              title   = "GitHub PR API"
              version = "1.0"
            }
            servers = [{ url = "https://api.github.com" }]
            paths = {
              "/repos/{owner}/{repo}/pulls/{pull_number}" = {
                get = {
                  operationId = "getPullRequest"
                  parameters = [
                    { name = "owner", in = "path", required = true, schema = { type = "string" } },
                    { name = "repo", in = "path", required = true, schema = { type = "string" } },
                    { name = "pull_number", in = "path", required = true, schema = { type = "integer" } }
                  ]
                  responses = { "200" = { description = "PR details" } }
                }
              }
              "/repos/{owner}/{repo}/pulls/{pull_number}/files" = {
                get = {
                  operationId = "listPullRequestFiles"
                  parameters = [
                    { name = "owner", in = "path", required = true, schema = { type = "string" } },
                    { name = "repo", in = "path", required = true, schema = { type = "string" } },
                    { name = "pull_number", in = "path", required = true, schema = { type = "integer" } }
                  ]
                  responses = { "200" = { description = "PR file diffs" } }
                }
              }
              "/repos/{owner}/{repo}/pulls/{pull_number}/reviews" = {
                post = {
                  operationId = "createPullRequestReview"
                  parameters = [
                    { name = "owner", in = "path", required = true, schema = { type = "string" } },
                    { name = "repo", in = "path", required = true, schema = { type = "string" } },
                    { name = "pull_number", in = "path", required = true, schema = { type = "integer" } }
                  ]
                  requestBody = {
                    required = true
                    content = {
                      "application/json" = {
                        schema = {
                          type = "object"
                          properties = {
                            body  = { type = "string" }
                            event = { type = "string", enum = ["APPROVE", "REQUEST_CHANGES", "COMMENT"] }
                          }
                        }
                      }
                    }
                  }
                  responses = { "200" = { description = "Review created" } }
                }
              }
            }
          })
        }
      }
    }
  }

  credential_provider_configuration {
    api_key {
      provider_arn              = aws_bedrockagentcore_api_key_credential_provider.github.credential_provider_arn
      credential_parameter_name = "Authorization"
      credential_prefix         = "token "
    }
  }
}
