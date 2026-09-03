# API Gateway REST API
resource "aws_api_gateway_rest_api" "mcp_api" {
  name        = "${local.lambda_name}-api"
  description = "API Gateway for OpenContext MCP Server"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# API Gateway Resource: /mcp
resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "mcp"
}

resource "aws_api_gateway_resource" "root_proxy" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "{proxy+}"
}

# API Gateway Resource: /.well-known/oauth-protected-resource
resource "aws_api_gateway_resource" "well_known" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = ".well-known"
}

resource "aws_api_gateway_resource" "oauth_protected_resource" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.well_known.id
  path_part   = "oauth-protected-resource"
}

resource "aws_api_gateway_resource" "oauth_protected_resource_proxy" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth_protected_resource.id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_resource" "oauth2" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "oauth2"
}

resource "aws_api_gateway_resource" "oauth2_auth" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth2.id
  path_part   = "auth"
}

resource "aws_api_gateway_resource" "oauth2_token" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth2.id
  path_part   = "token"
}

resource "aws_api_gateway_resource" "oauth2_callback" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth2.id
  path_part   = "callback"
}

resource "aws_api_gateway_resource" "oauth2_continue" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth2.id
  path_part   = "continue"
}

resource "aws_api_gateway_resource" "oauth2_register" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_resource.oauth2.id
  path_part   = "register"
}

# API Gateway Method: POST
resource "aws_api_gateway_method" "mcp_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: OPTIONS (for CORS, no API key required)
resource "aws_api_gateway_method" "mcp_options" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "OPTIONS"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: GET /.well-known/oauth-protected-resource
resource "aws_api_gateway_method" "oauth_protected_resource_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth_protected_resource.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: GET /.well-known/oauth-protected-resource/{proxy+}
resource "aws_api_gateway_method" "oauth_protected_resource_proxy_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth_protected_resource_proxy.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_method" "root_proxy_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.root_proxy.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_method" "root_proxy_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.root_proxy.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_method" "oauth2_auth_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth2_auth.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_method" "oauth2_token_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth2_token.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_method" "oauth2_callback_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth2_callback.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_method" "oauth2_continue_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth2_continue.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_method" "oauth2_register_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.oauth2_register.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false
}

# Lambda Integration for POST
resource "aws_api_gateway_integration" "mcp_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Lambda Integration for OAuth protected resource metadata
resource "aws_api_gateway_integration" "oauth_protected_resource_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth_protected_resource.id
  http_method = aws_api_gateway_method.oauth_protected_resource_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "oauth_protected_resource_proxy_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth_protected_resource_proxy.id
  http_method = aws_api_gateway_method.oauth_protected_resource_proxy_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn

  request_parameters = {
    "integration.request.path.proxy" = "method.request.path.proxy"
  }
}

resource "aws_api_gateway_integration" "root_proxy_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.root_proxy.id
  http_method = aws_api_gateway_method.root_proxy_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn

  request_parameters = {
    "integration.request.path.proxy" = "method.request.path.proxy"
  }
}

resource "aws_api_gateway_integration" "root_proxy_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.root_proxy.id
  http_method = aws_api_gateway_method.root_proxy_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn

  request_parameters = {
    "integration.request.path.proxy" = "method.request.path.proxy"
  }
}

resource "aws_api_gateway_integration" "oauth2_auth_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth2_auth.id
  http_method = aws_api_gateway_method.oauth2_auth_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "oauth2_token_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth2_token.id
  http_method = aws_api_gateway_method.oauth2_token_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "oauth2_callback_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth2_callback.id
  http_method = aws_api_gateway_method.oauth2_callback_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "oauth2_continue_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth2_continue.id
  http_method = aws_api_gateway_method.oauth2_continue_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "oauth2_register_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.oauth2_register.id
  http_method = aws_api_gateway_method.oauth2_register_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Lambda Integration for OPTIONS (mock response for CORS)
resource "aws_api_gateway_integration" "mcp_options_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_options.http_method

  type = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

# Method Response for POST
resource "aws_api_gateway_method_response" "mcp_post_response_200" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_post.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
  }
}

# Method Response for OPTIONS
resource "aws_api_gateway_method_response" "mcp_options_response_200" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
  }
}

# Integration Response for OPTIONS
resource "aws_api_gateway_integration_response" "mcp_options_integration_response" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_options.http_method
  status_code = aws_api_gateway_method_response.mcp_options_response_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type'"
    "method.response.header.Access-Control-Allow-Methods" = "'OPTIONS,POST'"
  }

  response_templates = {
    "application/json" = ""
  }

  depends_on = [aws_api_gateway_integration.mcp_options_integration]
}

# Lambda Permission for API Gateway
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_server.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.mcp_api.execution_arn}/*/*"
}

# API Gateway Deployment
resource "aws_api_gateway_deployment" "mcp_deployment" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.mcp.id,
      aws_api_gateway_resource.root_proxy.id,
      aws_api_gateway_resource.oauth_protected_resource.id,
      aws_api_gateway_resource.oauth_protected_resource_proxy.id,
      aws_api_gateway_resource.oauth2.id,
      aws_api_gateway_resource.oauth2_auth.id,
      aws_api_gateway_resource.oauth2_token.id,
      aws_api_gateway_resource.oauth2_callback.id,
      aws_api_gateway_resource.oauth2_continue.id,
      aws_api_gateway_resource.oauth2_register.id,
      aws_api_gateway_method.mcp_post.id,
      aws_api_gateway_method.mcp_options.id,
      aws_api_gateway_method.root_proxy_get.id,
      aws_api_gateway_method.root_proxy_post.id,
      aws_api_gateway_method.oauth_protected_resource_get.id,
      aws_api_gateway_method.oauth_protected_resource_proxy_get.id,
      aws_api_gateway_method.oauth2_auth_get.id,
      aws_api_gateway_method.oauth2_token_post.id,
      aws_api_gateway_method.oauth2_callback_get.id,
      aws_api_gateway_method.oauth2_continue_get.id,
      aws_api_gateway_method.oauth2_register_post.id,
      aws_api_gateway_integration.mcp_post_integration.id,
      aws_api_gateway_integration.mcp_options_integration.id,
      aws_api_gateway_integration.root_proxy_get_integration.id,
      aws_api_gateway_integration.root_proxy_post_integration.id,
      aws_api_gateway_integration.oauth_protected_resource_get_integration.id,
      aws_api_gateway_integration.oauth_protected_resource_proxy_get_integration.id,
      aws_api_gateway_integration.oauth2_auth_get_integration.id,
      aws_api_gateway_integration.oauth2_token_post_integration.id,
      aws_api_gateway_integration.oauth2_callback_get_integration.id,
      aws_api_gateway_integration.oauth2_continue_get_integration.id,
      aws_api_gateway_integration.oauth2_register_post_integration.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.mcp_post,
    aws_api_gateway_method.mcp_options,
    aws_api_gateway_method.root_proxy_get,
    aws_api_gateway_method.root_proxy_post,
    aws_api_gateway_method.oauth_protected_resource_get,
    aws_api_gateway_method.oauth_protected_resource_proxy_get,
    aws_api_gateway_method.oauth2_auth_get,
    aws_api_gateway_method.oauth2_token_post,
    aws_api_gateway_method.oauth2_callback_get,
    aws_api_gateway_method.oauth2_continue_get,
    aws_api_gateway_method.oauth2_register_post,
    aws_api_gateway_integration.mcp_post_integration,
    aws_api_gateway_integration.mcp_options_integration,
    aws_api_gateway_integration.root_proxy_get_integration,
    aws_api_gateway_integration.root_proxy_post_integration,
    aws_api_gateway_integration.oauth_protected_resource_get_integration,
    aws_api_gateway_integration.oauth_protected_resource_proxy_get_integration,
    aws_api_gateway_integration.oauth2_auth_get_integration,
    aws_api_gateway_integration.oauth2_token_post_integration,
    aws_api_gateway_integration.oauth2_callback_get_integration,
    aws_api_gateway_integration.oauth2_continue_get_integration,
    aws_api_gateway_integration.oauth2_register_post_integration,
    aws_api_gateway_method_response.mcp_post_response_200,
    aws_api_gateway_method_response.mcp_options_response_200,
    # aws_api_gateway_integration_response.mcp_post_integration_response,  # Removed - AWS_PROXY ignores it
    aws_api_gateway_integration_response.mcp_options_integration_response,
  ]
}

# API Gateway Stage
resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.mcp_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.mcp_api.id
  stage_name    = var.stage_name

  xray_tracing_enabled = true
}

# Method Settings: Throttling for all methods in stage (AWS format: */* not /*/*)
resource "aws_api_gateway_method_settings" "mcp_post" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

# Usage Plan
resource "aws_api_gateway_usage_plan" "mcp_usage_plan" {
  name = "${local.lambda_name}-usage-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.mcp_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  quota_settings {
    limit  = var.api_quota_limit
    period = "DAY"
  }

  throttle_settings {
    burst_limit = var.api_burst_limit
    rate_limit  = var.api_rate_limit
  }
}
