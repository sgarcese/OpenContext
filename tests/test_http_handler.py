"""Comprehensive tests for Universal HTTP Handler.

These tests verify HTTP request processing, path/method validation,
CORS handling, error handling, and server initialization.
"""

import pytest
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from server.http_handler import UniversalHTTPHandler, _initialize_server, _load_config
from core.validators import ConfigurationError
from server.auth import AuthResult


@pytest.fixture(autouse=True)
def disable_auth_by_default():
    """Keep legacy handler tests focused on HTTP behavior, not auth config."""
    import server.http_handler

    previous_config = server.http_handler._config
    server.http_handler._config = {
        "plugins": {"ckan": {"enabled": True}},
        "auth": {"enabled": False},
    }
    yield
    server.http_handler._config = previous_config


class TestPathValidation:
    """Test path validation."""

    @pytest.mark.asyncio
    async def test_valid_path_mcp_succeeds(self):
        """Test that /mcp path succeeds."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server") as mock_init,
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 200
            mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_path_returns_404(self):
        """Test that invalid path returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/invalid",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404
        assert headers["Content-Type"] == "application/json"
        error_body = json.loads(body)
        assert error_body["error"]["code"] == -32601
        assert error_body["error"]["message"] == "Not Found"
        assert "/invalid" in error_body["error"]["data"]

    @pytest.mark.asyncio
    async def test_root_path_returns_404(self):
        """Test that root path returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404

    @pytest.mark.asyncio
    async def test_mcp_with_trailing_slash_returns_404(self):
        """Test that /mcp/ (with trailing slash) returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/mcp/",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404


class TestMethodValidation:
    """Test HTTP method validation."""

    @pytest.mark.asyncio
    async def test_post_method_succeeds(self):
        """Test that POST method succeeds."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 200

    @pytest.mark.asyncio
    async def test_get_method_returns_405(self):
        """Test that GET method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="GET",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405
        assert headers["Allow"] == "POST"
        error_body = json.loads(body)
        assert error_body["error"]["code"] == -32601
        assert error_body["error"]["message"] == "Method Not Allowed"

    @pytest.mark.asyncio
    async def test_put_method_returns_405(self):
        """Test that PUT method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="PUT",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405

    @pytest.mark.asyncio
    async def test_delete_method_returns_405(self):
        """Test that DELETE method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="DELETE",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405


class TestCORS:
    """Test CORS handling."""

    @pytest.mark.asyncio
    async def test_cors_headers_added_to_response(self):
        """Test that CORS headers are added to response."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert headers["Access-Control-Allow-Origin"] == "*"
            assert headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
            assert headers["Access-Control-Allow-Headers"] == "authorization, content-type"

    def test_handle_options_returns_cors_headers(self):
        """Test that OPTIONS handler returns CORS headers."""
        handler = UniversalHTTPHandler()

        status, headers, body = handler.handle_options()

        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
        assert headers["Access-Control-Allow-Headers"] == "authorization, content-type"
        assert headers["Access-Control-Max-Age"] == "86400"
        assert body == ""


class TestOAuthAuth:
    """Test OAuth discovery and bearer-token enforcement."""

    auth_config = {
        "plugins": {"ckan": {"enabled": True}},
        "auth": {
            "enabled": True,
            "resource": "https://data-mcp-staging.boston.gov/mcp",
            "authorization_servers": ["https://auth.example.gov"],
            "scopes_supported": ["openid", "profile"],
            "required_scopes": ["openid"],
            "jwt": {
                "issuer": "https://auth.example.gov",
                "jwks_uri": "https://auth.example.gov/.well-known/jwks.json",
                "audience": "https://data-mcp-staging.boston.gov/mcp",
            },
        },
    }

    @pytest.mark.asyncio
    async def test_protected_resource_metadata_endpoint(self):
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._load_config", return_value=self.auth_config):
            status, headers, body = await handler.handle_request(
                method="GET",
                path="/.well-known/oauth-protected-resource/mcp",
                body="",
                headers={},
                request_id="test-request-id",
            )

        assert status == 200
        assert headers["Content-Type"] == "application/json"
        metadata = json.loads(body)
        assert metadata["resource"] == "https://data-mcp-staging.boston.gov/mcp"
        assert metadata["authorization_servers"] == ["https://auth.example.gov"]

    @pytest.mark.asyncio
    async def test_mcp_without_bearer_token_returns_www_authenticate(self):
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._load_config", return_value=self.auth_config):
            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
                request_id="test-request-id",
            )

        assert status == 401
        assert "www-authenticate" in headers
        assert (
            'resource_metadata="https://data-mcp-staging.boston.gov'
            '/.well-known/oauth-protected-resource/mcp"'
            in headers["www-authenticate"]
        )
        assert json.loads(body)["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_oauth_authorize_redirects_to_upstream_with_proxy_callback(self):
        handler = UniversalHTTPHandler()
        proxy_config = {
            "plugins": {"ckan": {"enabled": True}},
            "auth": {
                "enabled": True,
                "resource": "https://data-mcp-staging.boston.gov/mcp",
                "authorization_servers": ["https://data-mcp-staging.boston.gov"],
                "scopes_supported": ["openid"],
                "required_scopes": ["openid"],
                "jwt": {
                    "issuer": "https://strivacity-test.boston.gov/",
                    "jwks_uri": "https://strivacity-test.boston.gov/.well-known/jwks.json",
                },
                "oauth_proxy": {
                    "enabled": True,
                    "authorization_endpoint": "https://strivacity-test.boston.gov/oauth2/auth",
                    "token_endpoint": "https://strivacity-test.boston.gov/oauth2/token",
                    "callback_url": "https://data-mcp-staging.boston.gov/oauth2/callback",
                    "client_id": "7dd7654fc96c4fe5a18e91daaaf54b0b",
                },
            },
        }
        query = (
            "response_type=code"
            "&client_id=claude-client"
            "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "&code_challenge=challenge"
            "&code_challenge_method=S256"
            "&state=claude-state"
            "&scope=openid"
            "&resource=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Fmcp"
        )

        with (
            patch("server.http_handler._load_config", return_value=proxy_config),
            patch("server.auth.persist_authorization_pending"),
        ):
            status, headers, body = await handler.handle_request(
                method="GET",
                path="/oauth2/auth",
                body="",
                headers={"accept": "text/html", "user-agent": "Mozilla/5.0"},
                request_id="test-request-id",
                query_string=query,
            )

        assert status == 302
        location = headers["Location"]
        assert location.startswith("https://strivacity-test.boston.gov/oauth2/auth?")
        assert (
            "redirect_uri=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Foauth2%2Fcallback"
            in location
        )
        assert "client_id=7dd7654fc96c4fe5a18e91daaaf54b0b" in location
        assert "claude.ai" not in location
        assert "Set-Cookie" in headers
        assert body == ""

    @pytest.mark.asyncio
    async def test_mcp_with_valid_bearer_token_succeeds(self):
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._load_config", return_value=self.auth_config),
            patch(
                "server.http_handler.validate_bearer_token",
                new=AsyncMock(return_value=AuthResult(valid=True)),
            ) as mock_validate,
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={"authorization": "Bearer test-token"},
                request_id="test-request-id",
            )

        assert status == 200
        assert json.loads(body)["result"] == "success"
        mock_validate.assert_awaited_once()


class TestSessionID:
    """Test session ID generation."""

    @pytest.mark.asyncio
    async def test_initialize_request_generates_session_id(self):
        """Test that initialize request generates session ID."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                ),
                headers={},
            )

            assert "Mcp-Session-Id" in headers
            assert headers["Mcp-Session-Id"] is not None
            assert len(headers["Mcp-Session-Id"]) > 0

    @pytest.mark.asyncio
    async def test_non_initialize_request_no_session_id(self):
        """Test that non-initialize request doesn't generate session ID."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "ping",
                        "params": {},
                    }
                ),
                headers={},
            )

            assert "Mcp-Session-Id" not in headers


class TestRequestID:
    """Test request ID handling."""

    @pytest.mark.asyncio
    async def test_request_id_added_to_response_headers(self):
        """Test that request ID is added to response headers."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
                request_id="test-request-id-123",
            )

            assert headers["X-Request-ID"] == "test-request-id-123"

    @pytest.mark.asyncio
    async def test_default_request_id_when_not_provided(self):
        """Test that default request ID is used when not provided."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
                # No request_id provided
            )

            assert "X-Request-ID" in headers
            assert headers["X-Request-ID"] == "unknown"


class TestErrorHandling:
    """Test error handling."""

    @pytest.mark.asyncio
    async def test_configuration_error_returns_500(self):
        """Test that ConfigurationError returns 500."""
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._initialize_server") as mock_init:
            mock_init.side_effect = ConfigurationError("Config error")

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 500
            error_body = json.loads(body)
            assert error_body["error"]["code"] == -32603
            assert error_body["error"]["message"] == "Server configuration error"
            assert "Config error" in error_body["error"]["data"]

    @pytest.mark.asyncio
    async def test_general_exception_returns_500(self):
        """Test that general exceptions return 500."""
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._initialize_server") as mock_init:
            mock_init.side_effect = Exception("Unexpected error")

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 500
            error_body = json.loads(body)
            assert error_body["error"]["code"] == -32603
            assert error_body["error"]["message"] == "Internal error"


class TestConfigLoading:
    """Test configuration loading."""

    def test_load_config_from_environment_variable(self):
        """Test loading config from environment variable."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": json.dumps(config_data)}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            config = _load_config()
            assert config["server_name"] == "TestServer"
            assert config["plugins"]["ckan"]["enabled"] is True

    def test_load_config_from_file_when_env_not_set(self, tmp_path):
        """Test loading config from file when environment variable not set."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        config_file = tmp_path / "config.yaml"
        import yaml

        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("server.http_handler.load_and_validate_config") as mock_load,
        ):
            mock_load.return_value = config_data

            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            _load_config()
            mock_load.assert_called_once_with("config.yaml")

    def test_load_config_raises_on_invalid_json(self):
        """Test that invalid JSON in environment variable raises error."""
        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": "invalid json"}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            with pytest.raises((ValueError, json.JSONDecodeError)):
                _load_config()

    def test_load_config_caches_result(self):
        """Test that config is cached after first load."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {"ckan": {"enabled": True}},
        }

        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": json.dumps(config_data)}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            config1 = _load_config()
            config2 = _load_config()

            # Should return same object (cached)
            assert config1 is config2


class TestServerInitialization:
    """Test server initialization."""

    @pytest.mark.asyncio
    async def test_initialize_server_creates_plugin_manager_and_mcp_server(self):
        """Test that server initialization creates plugin manager and MCP server."""
        config = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
            patch("server.http_handler.MCPServer") as mock_mcp_class,
        ):
            mock_load_config.return_value = config
            mock_pm = MagicMock()
            mock_pm.load_plugins = AsyncMock()
            mock_pm_class.return_value = mock_pm
            mock_mcp = MagicMock()
            mock_mcp_class.return_value = mock_mcp

            # Clear global state
            import server.http_handler

            server.http_handler._plugin_manager = None
            server.http_handler._mcp_server = None

            await _initialize_server()

            mock_pm_class.assert_called_once_with(config)
            mock_pm.load_plugins.assert_called_once()
            mock_mcp_class.assert_called_once_with(mock_pm)

    @pytest.mark.asyncio
    async def test_initialize_server_reuses_existing_instances(self):
        """Test that server initialization reuses existing instances."""
        config = {"plugins": {"ckan": {"enabled": True}}}

        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
        ):
            mock_load_config.return_value = config

            # Set existing instances
            import server.http_handler

            server.http_handler._plugin_manager = MagicMock()
            server.http_handler._mcp_server = MagicMock()

            await _initialize_server()

            # Should not create new instances
            mock_pm_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_server_raises_on_configuration_error(self):
        """Test that server initialization raises on configuration error."""
        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
        ):
            from core.validators import ConfigurationError

            mock_load_config.return_value = {"plugins": {}}
            mock_pm = MagicMock()
            mock_pm.load_plugins = AsyncMock(
                side_effect=ConfigurationError("Config error")
            )
            mock_pm_class.return_value = mock_pm

            # Clear global state
            import server.http_handler

            server.http_handler._plugin_manager = None
            server.http_handler._mcp_server = None

            with pytest.raises(RuntimeError) as exc_info:
                await _initialize_server()

            assert "Configuration error" in str(exc_info.value)
