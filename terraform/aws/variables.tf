variable "lambda_name" {
  description = "Name of the Lambda function"
  type        = string
  default     = "opencontext-mcp-server"
}

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "config_file" {
  description = "Path to config.yaml file"
  type        = string
  default     = "../../config.yaml"
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 120
}

variable "api_quota_limit" {
  description = "API Gateway daily request quota"
  type        = number
  default     = 1000
}

variable "api_rate_limit" {
  description = "API Gateway requests per second rate limit"
  type        = number
  default     = 5
}

variable "api_burst_limit" {
  description = "API Gateway burst limit"
  type        = number
  default     = 10
}

variable "stage_name" {
  description = "API Gateway stage name (e.g. prod, dev, staging)"
  type        = string
  default     = "staging"
}

variable "custom_domain" {
  description = "Custom domain name for API Gateway (leave empty to skip custom domain setup)"
  type        = string
  default     = ""
}

variable "oauth_idp_host" {
  description = "Strivacity/OIDC hostname (e.g. strivacity-test.boston.gov). Derives issuer, JWKS, authorize, token, and userinfo URLs when those vars are empty."
  type        = string
  default     = ""
}

variable "oauth_issuer" {
  description = "OAuth authorization server issuer URL for MCP protected resource metadata"
  type        = string
  default     = ""
}

variable "oauth_jwks_uri" {
  description = "OIDC JWKS URI used by the Lambda to validate JWT bearer tokens"
  type        = string
  default     = ""
}

variable "oauth_audience" {
  description = "Expected JWT audience for bearer tokens; leave empty to skip audience validation"
  type        = string
  default     = ""
}

variable "oauth_authorization_endpoint" {
  description = "Upstream OAuth authorization endpoint used by the MCP OAuth proxy"
  type        = string
  default     = ""
}

variable "oauth_token_endpoint" {
  description = "Upstream OAuth token endpoint used by the MCP OAuth proxy"
  type        = string
  default     = ""
}

variable "oauth_userinfo_endpoint" {
  description = "Upstream OIDC userinfo endpoint used to validate opaque access tokens"
  type        = string
  default     = ""
}

variable "oauth_client_id" {
  description = "Upstream OAuth client ID used by the MCP OAuth proxy"
  type        = string
  default     = ""
}

variable "oauth_client_secret" {
  description = "Upstream OAuth client secret used by the MCP OAuth proxy"
  type        = string
  default     = ""
  sensitive   = true
}
