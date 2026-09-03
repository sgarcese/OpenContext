#!/bin/bash
# OpenContext Deployment Script
#
# This script validates configuration and deploys the MCP server to AWS Lambda.
# It enforces the "one fork = one MCP server" rule.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Script directory (scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Project root (parent directory)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Parse named arguments
ENVIRONMENT=""
TF_WORKSPACE=""

show_usage() {
    echo "Usage: $0 --environment <staging|prod> [--tfworkspace <name>]"
    echo ""
    echo "Options:"
    echo "  --environment, -e   Deployment environment: staging or prod (required)"
    echo "  --tfworkspace, -w   Terraform workspace name (default: boston-staging or boston-prod)"
    echo "  --help, -h          Show this help message"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment|-e)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --tfworkspace|-w)
            TF_WORKSPACE="$2"
            shift 2
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        *)
            echo -e "${RED}❌ Error: Unknown argument '${1}'${NC}"
            show_usage
            exit 1
            ;;
    esac
done

if [ -z "$ENVIRONMENT" ]; then
    echo -e "${RED}❌ Error: --environment is required${NC}"
    show_usage
    exit 1
fi

if [ "$ENVIRONMENT" != "staging" ] && [ "$ENVIRONMENT" != "prod" ]; then
    echo -e "${RED}❌ Error: Invalid environment '${ENVIRONMENT}'. Must be 'staging' or 'prod'.${NC}"
    show_usage
    exit 1
fi

# Default workspace per environment when not explicitly provided
if [ -z "$TF_WORKSPACE" ]; then
    if [ "$ENVIRONMENT" = "prod" ]; then
        TF_WORKSPACE="boston-prod"
    else
        TF_WORKSPACE="boston-staging"
    fi
fi

echo -e "${GREEN}🚀 OpenContext Deployment [${ENVIRONMENT}] (workspace: ${TF_WORKSPACE})${NC}"
echo "================================"
echo ""

# Check if config.yaml exists
if [ ! -f "config.yaml" ]; then
    echo -e "${RED}❌ Error: config.yaml not found${NC}"
    echo "Create config from template: cp config-example.yaml config.yaml"
    echo "Then edit config.yaml and enable exactly ONE plugin."
    exit 1
fi

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Error: python3 not found${NC}"
    echo "Please install Python 3.11 or later."
    exit 1
fi

# Check if Terraform is available
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}❌ Error: terraform not found${NC}"
    echo "Please install Terraform: https://www.terraform.io/downloads"
    exit 1
fi

echo -e "${YELLOW}📋 Step 1: Validating configuration...${NC}"

# Count enabled plugins using Python for reliable YAML parsing
ENABLED_COUNT=$(python3 << 'EOF'
import yaml
import sys

try:
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    plugins = config.get('plugins', {})
    enabled = []

    for plugin_name, plugin_config in plugins.items():
        if isinstance(plugin_config, dict) and plugin_config.get('enabled', False):
            enabled.append(plugin_name)

    count = len(enabled)

    if count == 0:
        print("0", file=sys.stderr)
        print("No plugins enabled", file=sys.stderr)
        sys.exit(1)
    elif count > 1:
        print(str(count), file=sys.stderr)
        print(" ".join(enabled), file=sys.stderr)
        sys.exit(2)
    else:
        print(count)
        print(enabled[0], file=sys.stderr)
        sys.exit(0)

except Exception as e:
    print(f"Error parsing config.yaml: {e}", file=sys.stderr)
    sys.exit(3)
EOF
)

EXIT_CODE=$?

if [ $EXIT_CODE -eq 1 ]; then
    echo -e "${RED}❌ Configuration Error: No Plugins Enabled${NC}"
    echo ""
    echo "You must enable exactly ONE plugin in config.yaml."
    echo ""
    echo "To enable a plugin, set 'enabled: true' for:"
    echo "  • ckan"
    echo "  • A custom plugin in custom_plugins/"
    echo ""
    echo "See docs/GETTING_STARTED.md for setup instructions."
    exit 1
elif [ $EXIT_CODE -eq 2 ]; then
    ENABLED_PLUGINS=$(python3 << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
plugins = config.get('plugins', {})
enabled = [name for name, cfg in plugins.items()
           if isinstance(cfg, dict) and cfg.get('enabled', False)]
print("\n".join(f"  • {name}" for name in enabled))
EOF
    )

    echo -e "${RED}❌ Configuration Error: Multiple Plugins Enabled${NC}"
    echo ""
    echo "You have $ENABLED_COUNT plugins enabled in config.yaml:"
    echo "$ENABLED_PLUGINS"
    echo ""
    echo "OpenContext enforces: One Fork = One MCP Server"
    echo ""
    echo "This keeps deployments:"
    echo "  ✓ Simple and focused"
    echo "  ✓ Independently scalable"
    echo "  ✓ Easy to maintain"
    echo ""
    echo "To deploy multiple MCP servers:"
    echo ""
    echo "  1. Fork this repository again"
    echo "     Example: opencontext-opendata, opencontext-mbta"
    echo ""
    echo "  2. Configure ONE plugin per fork"
    FIRST_PLUGIN=$(echo "$ENABLED_PLUGINS" | head -n1 | sed 's/  • //')
    SECOND_PLUGIN=$(echo "$ENABLED_PLUGINS" | tail -n1 | sed 's/  • //')
    echo "     Fork #1: Enable $FIRST_PLUGIN only"
    echo "     Fork #2: Enable $SECOND_PLUGIN only"
    echo ""
    echo "  3. Deploy each fork separately"
    echo "     ./scripts/deploy.sh (in each fork)"
    echo ""
    echo "See docs/ARCHITECTURE.md for details."
    exit 1
elif [ $EXIT_CODE -ne 0 ]; then
    echo -e "${RED}❌ Error validating configuration${NC}"
    exit 1
fi

ENABLED_PLUGIN=$(python3 << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
plugins = config.get('plugins', {})
enabled = [name for name, cfg in plugins.items()
           if isinstance(cfg, dict) and cfg.get('enabled', False)]
print(enabled[0])
EOF
)

echo -e "${GREEN}✓ Configuration valid: ${ENABLED_PLUGIN} plugin enabled${NC}"
echo ""

# Extract server name and AWS settings
SERVER_NAME=$(python3 << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
print(config.get('server_name', 'my-mcp-server'))
EOF
)

AWS_REGION=$(python3 << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
print(config.get('aws', {}).get('region', 'us-east-1'))
EOF
)

TFVARS_FILE="terraform/aws/${ENVIRONMENT}.tfvars"
LAMBDA_NAME=$(grep '^lambda_name' "$TFVARS_FILE" | sed 's/.*=\s*"\(.*\)"/\1/')

echo -e "${YELLOW}📦 Step 2: Packaging Lambda code...${NC}"

# Create deployment package directory
PACKAGE_DIR=".deploy"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Copy code to package directory
cp -r core "$PACKAGE_DIR/"
cp -r plugins "$PACKAGE_DIR/"
cp -r custom_plugins "$PACKAGE_DIR/" 2>/dev/null || mkdir -p "$PACKAGE_DIR/custom_plugins"
cp -r server "$PACKAGE_DIR/"
cp requirements.txt "$PACKAGE_DIR/" 2>/dev/null || true

# Install Python dependencies into package directory
echo "Installing Python dependencies..."

# Prefer uv for faster, cached installs; fall back to pip if uv is unavailable
if command -v uv &> /dev/null; then
    echo "Using uv to install dependencies..."
    if ! uv pip install -r requirements.txt \
        --target "$PACKAGE_DIR/" \
        --python-platform x86_64-manylinux2014 \
        --python-version 3.11 \
        --no-compile; then
        echo -e "${RED}❌ Error: Failed to install dependencies with uv${NC}"
        exit 1
    fi
else
    echo "uv not found, falling back to pip..."
    if ! pip install -r requirements.txt -t "$PACKAGE_DIR/" --platform manylinux2014_x86_64 --only-binary :all: --no-compile --no-deps 2>/dev/null; then
        echo "Platform-specific install failed, trying generic install..."
        if ! pip install -r requirements.txt -t "$PACKAGE_DIR/" --no-compile 2>/dev/null; then
            echo -e "${RED}❌ Error: Failed to install dependencies${NC}"
            echo "Please ensure pip is available and requirements.txt is valid."
            exit 1
        fi
    fi
fi

# Create zip file
ZIP_FILE="lambda-deployment.zip"
cd "$PACKAGE_DIR"
zip -r "../$ZIP_FILE" . > /dev/null
cd ..

echo -e "${GREEN}✓ Lambda package created: $ZIP_FILE${NC}"
echo ""

echo -e "${YELLOW}🏗️  Step 3: Deploying with Terraform...${NC}"

# Copy zip file and config.yaml to Terraform module directory
cp "$ZIP_FILE" terraform/aws/lambda-deployment.zip
cp config.yaml terraform/aws/config.yaml

# Initialize Terraform if needed
if [ ! -d "terraform/aws/.terraform" ]; then
    echo "Initializing Terraform..."
    cd terraform/aws
    terraform init
    cd ../..
fi

# Plan first - validates configuration and catches errors before any changes
cd terraform/aws

# Per-environment OAuth credentials. Staging and prod each have their own
# Strivacity client; never reuse a leftover generic OAUTH_CLIENT_* across envs.
ENV_KEY="$(echo "$ENVIRONMENT" | tr '[:lower:]' '[:upper:]')"
SECRETS_FILE="secrets.${ENVIRONMENT}.tfvars"
DOTENV_FILE="${PROJECT_ROOT}/.env.${ENVIRONMENT}"

if [ -f "$DOTENV_FILE" ]; then
    echo "Loading OAuth credentials from .env.${ENVIRONMENT}"
    set -a
    # shellcheck disable=SC1090
    source "$DOTENV_FILE"
    set +a
fi

specific_client_id_var="OAUTH_CLIENT_ID_${ENV_KEY}"
specific_client_secret_var="OAUTH_CLIENT_SECRET_${ENV_KEY}"
OAUTH_CLIENT_ID_RESOLVED="${!specific_client_id_var:-}"
OAUTH_CLIENT_SECRET_RESOLVED="${!specific_client_secret_var:-}"

if [ -z "$OAUTH_CLIENT_ID_RESOLVED" ] && [ -n "${OAUTH_CLIENT_ID:-}" ]; then
    echo -e "${YELLOW}⚠️  Using generic OAUTH_CLIENT_ID for ${ENVIRONMENT}. Prefer OAUTH_CLIENT_ID_${ENV_KEY} or ${SECRETS_FILE}.${NC}"
    OAUTH_CLIENT_ID_RESOLVED="$OAUTH_CLIENT_ID"
fi
if [ -z "$OAUTH_CLIENT_SECRET_RESOLVED" ] && [ -n "${OAUTH_CLIENT_SECRET:-}" ]; then
    echo -e "${YELLOW}⚠️  Using generic OAUTH_CLIENT_SECRET for ${ENVIRONMENT}. Prefer OAUTH_CLIENT_SECRET_${ENV_KEY} or ${SECRETS_FILE}.${NC}"
    OAUTH_CLIENT_SECRET_RESOLVED="$OAUTH_CLIENT_SECRET"
fi

TF_VAR_FILE_ARGS=("-var-file=${ENVIRONMENT}.tfvars")
if [ -f "$SECRETS_FILE" ]; then
    echo "Loading OAuth credentials from ${SECRETS_FILE}"
    TF_VAR_FILE_ARGS+=("-var-file=${SECRETS_FILE}")
fi

TF_CLI_VAR_ARGS=()
if [ -n "$OAUTH_CLIENT_ID_RESOLVED" ]; then
    TF_CLI_VAR_ARGS+=("-var=oauth_client_id=${OAUTH_CLIENT_ID_RESOLVED}")
fi
if [ -n "$OAUTH_CLIENT_SECRET_RESOLVED" ]; then
    TF_CLI_VAR_ARGS+=("-var=oauth_client_secret=${OAUTH_CLIENT_SECRET_RESOLVED}")
fi

oauth_configured=false
if grep -Eq '^[[:space:]]*(oauth_issuer|oauth_idp_host|oauth_client_id)[[:space:]]*=' "${ENVIRONMENT}.tfvars"; then
    oauth_configured=true
fi
if [ -f "$SECRETS_FILE" ] && grep -Eq '^[[:space:]]*(oauth_issuer|oauth_idp_host|oauth_client_id)[[:space:]]*=' "$SECRETS_FILE"; then
    oauth_configured=true
fi
has_oauth_secret=false
if [ -n "$OAUTH_CLIENT_SECRET_RESOLVED" ]; then
    has_oauth_secret=true
fi
if [ -f "$SECRETS_FILE" ] && grep -Eq '^[[:space:]]*oauth_client_secret[[:space:]]*=' "$SECRETS_FILE"; then
    has_oauth_secret=true
fi
if [ "$oauth_configured" = true ] && [ "$has_oauth_secret" = false ]; then
    echo -e "${RED}❌ OAuth is configured for ${ENVIRONMENT} but no client secret was found.${NC}"
    echo "Create terraform/aws/${SECRETS_FILE} from terraform/aws/secrets.tfvars.example"
    echo "or export ${specific_client_secret_var}."
    exit 1
fi

# Optional OIDC metadata overrides. Environment tfvars are the source of truth;
# only export when the operator explicitly set a value so empty TF_VAR_* cannot
# clobber staging vs prod endpoints.
export TF_VAR_oauth_issuer="${TF_VAR_oauth_issuer:-${OAUTH_ISSUER:-}}"
export TF_VAR_oauth_jwks_uri="${TF_VAR_oauth_jwks_uri:-${OAUTH_JWKS_URI:-}}"
export TF_VAR_oauth_audience="${TF_VAR_oauth_audience:-${OAUTH_AUDIENCE:-}}"
export TF_VAR_oauth_authorization_endpoint="${TF_VAR_oauth_authorization_endpoint:-${OAUTH_AUTHORIZATION_ENDPOINT:-}}"
export TF_VAR_oauth_token_endpoint="${TF_VAR_oauth_token_endpoint:-${OAUTH_TOKEN_ENDPOINT:-}}"
export TF_VAR_oauth_userinfo_endpoint="${TF_VAR_oauth_userinfo_endpoint:-${OAUTH_USERINFO_ENDPOINT:-}}"
if [ -z "${TF_VAR_oauth_issuer:-}" ]; then unset TF_VAR_oauth_issuer; fi
if [ -z "${TF_VAR_oauth_jwks_uri:-}" ]; then unset TF_VAR_oauth_jwks_uri; fi
if [ -z "${TF_VAR_oauth_audience:-}" ]; then unset TF_VAR_oauth_audience; fi
if [ -z "${TF_VAR_oauth_authorization_endpoint:-}" ]; then unset TF_VAR_oauth_authorization_endpoint; fi
if [ -z "${TF_VAR_oauth_token_endpoint:-}" ]; then unset TF_VAR_oauth_token_endpoint; fi
if [ -z "${TF_VAR_oauth_userinfo_endpoint:-}" ]; then unset TF_VAR_oauth_userinfo_endpoint; fi

echo "Selecting Terraform workspace: ${TF_WORKSPACE}"
terraform workspace select "$TF_WORKSPACE" 2>/dev/null || terraform workspace new "$TF_WORKSPACE"

echo -e "${YELLOW}📋 Planning Terraform changes...${NC}"
PLAN_ARGS=(
    -out=tfplan
    "${TF_VAR_FILE_ARGS[@]}"
    -var="aws_region=$AWS_REGION"
    -var="config_file=config.yaml"
)
if [ ${#TF_CLI_VAR_ARGS[@]} -gt 0 ]; then
    PLAN_ARGS+=("${TF_CLI_VAR_ARGS[@]}")
fi
if ! terraform plan "${PLAN_ARGS[@]}"; then
    echo -e "${RED}❌ Terraform plan failed - aborting deployment${NC}"
    exit 1
fi

# Require explicit approval before deploying
echo ""
echo -e "${YELLOW}⚠️  Deployment will apply the planned changes to AWS.${NC}"
echo -e "   Environment: ${ENVIRONMENT}"
echo -e "   Workspace:   ${TF_WORKSPACE}"
echo -e "   Lambda:      ${LAMBDA_NAME}"
echo -e "   Region:      ${AWS_REGION}"
echo ""
read -r -p "Do you want to proceed with deployment? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ] && [ "$CONFIRM" != "y" ]; then
    echo -e "${YELLOW}Deployment cancelled by user.${NC}"
    rm -f tfplan
    exit 0
fi
echo ""

# Apply the planned changes
echo -e "${YELLOW}🚀 Applying Terraform changes...${NC}"
terraform apply tfplan
rm -f tfplan

# Get URLs from Terraform output
LAMBDA_URL=$(terraform output -raw lambda_url 2>/dev/null || echo "")
API_GATEWAY_URL=$(terraform output -raw api_gateway_url 2>/dev/null || echo "")

cd ..

echo ""
echo -e "${GREEN}✅ Deployment complete!${NC}"
echo ""
echo "API Gateway URL (use for Claude Connectors):"
echo -e "${GREEN}$API_GATEWAY_URL${NC}"
echo ""
echo "Lambda Function URL (for direct HTTP testing):"
echo -e "${GREEN}$LAMBDA_URL${NC}"
echo ""
echo "Connect via Claude Connectors (same on Claude.ai and Claude Desktop):"
echo "  1. Go to Settings → Connectors (or Customize → Connectors on claude.ai)"
echo "  2. Click 'Add custom connector'"
echo "  3. Enter a name and URL: $API_GATEWAY_URL"
echo ""
