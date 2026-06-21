resource "aws_iam_role" "demo_app_role" {
  name = "demo-app-service-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "demo_app_full_access" {
  name = "demo-app-full-access"
  role = aws_iam_role.demo_app_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "*"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateUser", "iam:AttachUserPolicy", "iam:CreateAccessKey"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_user" "demo_service_account" {
  name = "demo-service-account"
}

resource "aws_iam_user_policy_attachment" "demo_admin" {
  user       = aws_iam_user.demo_service_account.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
