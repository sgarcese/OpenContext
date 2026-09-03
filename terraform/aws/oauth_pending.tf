resource "aws_dynamodb_table" "oauth_pending" {
  name         = "${local.lambda_name}-oauth-pending"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_iam_role_policy" "lambda_oauth_pending" {
  name = "${local.lambda_name}-oauth-pending-dynamodb"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
        ]
        Resource = aws_dynamodb_table.oauth_pending.arn
      },
    ]
  })
}
