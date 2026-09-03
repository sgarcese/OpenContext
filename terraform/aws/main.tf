terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket  = "opencontext-terraform-state"
    key     = "opencontext/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Read config.yaml
locals {
  config = yamldecode(file(var.config_file))

  lambda_name = var.lambda_name != "" ? var.lambda_name : (
    local.config.server_name != "" ? lower(replace(local.config.server_name, " ", "-")) : "opencontext-mcp-server"
  )

  lambda_memory  = local.config.aws.lambda_memory != null ? local.config.aws.lambda_memory : var.lambda_memory
  lambda_timeout = local.config.aws.lambda_timeout != null ? local.config.aws.lambda_timeout : var.lambda_timeout

  # MCP public origin from the environment tfvars custom_domain. config.yaml
  # can reference these via ${OAUTH_RESOURCE} / ${OAUTH_CALLBACK_URL} /
  # ${OAUTH_AUTHORIZATION_SERVER} so one config works for staging and prod.
  mcp_origin                 = var.custom_domain != "" ? "https://${trimsuffix(var.custom_domain, "/")}" : ""
  oauth_resource             = local.mcp_origin != "" ? "${local.mcp_origin}/mcp" : ""
  oauth_callback_url         = local.mcp_origin != "" ? "${local.mcp_origin}/oauth2/callback" : ""
  oauth_authorization_server = local.mcp_origin

  # IdP hostname lives in secrets.<env>.tfvars. Standard Strivacity paths
  # are derived unless an explicit URL override is set.
  oauth_idp_host_normalized = trimsuffix(
    replace(replace(var.oauth_idp_host, "https://", ""), "http://", ""),
    "/"
  )
  oauth_idp_origin = local.oauth_idp_host_normalized != "" ? "https://${local.oauth_idp_host_normalized}" : ""
  oauth_issuer = var.oauth_issuer != "" ? var.oauth_issuer : (
    local.oauth_idp_origin != "" ? "${local.oauth_idp_origin}/" : ""
  )
  oauth_jwks_uri = var.oauth_jwks_uri != "" ? var.oauth_jwks_uri : (
    local.oauth_idp_origin != "" ? "${local.oauth_idp_origin}/.well-known/jwks.json" : ""
  )
  oauth_authorization_endpoint = var.oauth_authorization_endpoint != "" ? var.oauth_authorization_endpoint : (
    local.oauth_idp_origin != "" ? "${local.oauth_idp_origin}/oauth2/auth" : ""
  )
  oauth_token_endpoint = var.oauth_token_endpoint != "" ? var.oauth_token_endpoint : (
    local.oauth_idp_origin != "" ? "${local.oauth_idp_origin}/oauth2/token" : ""
  )
  oauth_userinfo_endpoint = var.oauth_userinfo_endpoint != "" ? var.oauth_userinfo_endpoint : (
    local.oauth_idp_origin != "" ? "${local.oauth_idp_origin}/userinfo" : ""
  )

  # Serialize config to JSON for environment variable
  config_json = jsonencode(local.config)
}

# IAM role for Lambda
resource "aws_iam_role" "lambda_role" {
  name = "${local.lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

# Basic Lambda execution policy
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Lambda deployment package (created by scripts/deploy.sh)
# deploy.sh builds .deploy/ and lambda-deployment.zip, then copies the zip here.
locals {
  lambda_zip_path = "${path.module}/lambda-deployment.zip"
  lambda_zip_hash = filebase64sha256(local.lambda_zip_path)
}

# Lambda function
resource "aws_lambda_function" "mcp_server" {
  filename         = local.lambda_zip_path
  function_name    = local.lambda_name
  role             = aws_iam_role.lambda_role.arn
  handler          = "server.adapters.aws_lambda.lambda_handler"
  source_code_hash = local.lambda_zip_hash
  runtime          = "python3.11"
  memory_size      = local.lambda_memory
  timeout          = local.lambda_timeout

  environment {
    variables = merge(
      {
        OPENCONTEXT_CONFIG = local.config_json
      },
      local.oauth_issuer != "" ? {
        OAUTH_ISSUER = local.oauth_issuer
      } : {},
      local.oauth_jwks_uri != "" ? {
        OAUTH_JWKS_URI = local.oauth_jwks_uri
      } : {},
      var.oauth_audience != "" ? {
        OAUTH_AUDIENCE = var.oauth_audience
      } : {},
      local.oauth_authorization_endpoint != "" ? {
        OAUTH_AUTHORIZATION_ENDPOINT = local.oauth_authorization_endpoint
      } : {},
      local.oauth_token_endpoint != "" ? {
        OAUTH_TOKEN_ENDPOINT = local.oauth_token_endpoint
      } : {},
      local.oauth_userinfo_endpoint != "" ? {
        OAUTH_USERINFO_ENDPOINT = local.oauth_userinfo_endpoint
      } : {},
      local.oauth_resource != "" ? {
        OAUTH_RESOURCE = local.oauth_resource
      } : {},
      local.oauth_callback_url != "" ? {
        OAUTH_CALLBACK_URL = local.oauth_callback_url
      } : {},
      local.oauth_authorization_server != "" ? {
        OAUTH_AUTHORIZATION_SERVER = local.oauth_authorization_server
      } : {},
      var.oauth_client_id != "" ? {
        OAUTH_CLIENT_ID = var.oauth_client_id
      } : {},
      var.oauth_client_secret != "" ? {
        OAUTH_CLIENT_SECRET = var.oauth_client_secret
      } : {},
      {
        OAUTH_PENDING_TABLE_NAME = aws_dynamodb_table.oauth_pending.name
      }
    )
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_iam_role_policy.lambda_oauth_pending,
  ]
}

# Lambda Function URL
resource "aws_lambda_function_url" "mcp_server_url" {
  function_name      = aws_lambda_function.mcp_server.function_name
  authorization_type = "NONE"

  cors {
    allow_origins  = ["*"]
    allow_methods  = ["POST"]
    allow_headers  = ["content-type"]
    expose_headers = ["x-request-id", "mcp-session-id"]
    max_age        = 86400
  }
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${local.lambda_name}"
  retention_in_days = 14
}
