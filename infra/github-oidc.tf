resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

resource "aws_iam_role" "github_actions_review" {
  name = "${var.project}-github-actions-review"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:IlaakshMishra/gatecheck:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_review" {
  name = "${var.project}-github-actions-review-policy"
  role = aws_iam_role.github_actions_review.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
      Resource = "*"
    }]
  })
}

output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions_review.arn
  description = "Set this as AWS_REVIEW_ROLE_ARN in GitHub repo secrets"
}
