"""Session-keyed store for OAuth pending state (browser/cookie split workaround)."""

import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OAUTH_PENDING_TABLE_NAME = os.environ.get("OAUTH_PENDING_TABLE_NAME", "")
DEFAULT_TTL_SECONDS = 600

_pending_memory: Dict[str, Tuple[str, float]] = {}
_dynamodb_client = None


def _get_dynamodb_client():
    global _dynamodb_client
    if _dynamodb_client is None:
        import boto3

        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def save_pending_proxy_state(
    session_id: str, proxy_state: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> None:
    if not session_id or not proxy_state:
        return
    expires_at = int(time.time()) + ttl_seconds
    if OAUTH_PENDING_TABLE_NAME:
        try:
            _get_dynamodb_client().put_item(
                TableName=OAUTH_PENDING_TABLE_NAME,
                Item={
                    "session_id": {"S": session_id},
                    "proxy_state": {"S": proxy_state},
                    "expires_at": {"N": str(expires_at)},
                },
            )
            logger.info(
                "OAuth pending state saved",
                extra={
                    "pending_key_kind": _pending_key_kind(session_id),
                    "pending_key": session_id,
                    "storage": "dynamodb",
                    "proxy_state_length": len(proxy_state),
                    "expires_at": expires_at,
                },
            )
            return
        except Exception as e:
            logger.warning(
                "Failed to save OAuth pending state to DynamoDB: %s",
                e,
                extra={"pending_key": session_id},
                exc_info=True,
            )
    _pending_memory[session_id] = (proxy_state, float(expires_at))
    logger.info(
        "OAuth pending state saved",
        extra={
            "pending_key_kind": _pending_key_kind(session_id),
            "pending_key": session_id,
            "storage": "memory",
            "proxy_state_length": len(proxy_state),
            "expires_at": expires_at,
        },
    )


def _pending_key_kind(session_id: str) -> str:
    if session_id == "__latest__":
        return "latest"
    if session_id.startswith("state:"):
        return "idp_state"
    return "session_id"


def get_pending_proxy_state(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    if OAUTH_PENDING_TABLE_NAME:
        try:
            response = _get_dynamodb_client().get_item(
                TableName=OAUTH_PENDING_TABLE_NAME,
                Key={"session_id": {"S": session_id}},
                ConsistentRead=True,
            )
            item = response.get("Item")
            if not item:
                logger.info(
                    "OAuth pending state miss",
                    extra={
                        "pending_key_kind": _pending_key_kind(session_id),
                        "pending_key": session_id,
                        "storage": "dynamodb",
                    },
                )
                return None
            expires_at = float(item.get("expires_at", {}).get("N", "0"))
            if expires_at and time.time() > expires_at:
                delete_pending_proxy_state(session_id)
                logger.info(
                    "OAuth pending state expired",
                    extra={
                        "pending_key_kind": _pending_key_kind(session_id),
                        "pending_key": session_id,
                        "storage": "dynamodb",
                    },
                )
                return None
            proxy_state = item.get("proxy_state", {}).get("S") or None
            logger.info(
                "OAuth pending state hit",
                extra={
                    "pending_key_kind": _pending_key_kind(session_id),
                    "pending_key": session_id,
                    "storage": "dynamodb",
                    "proxy_state_length": len(proxy_state or ""),
                },
            )
            return proxy_state
        except Exception as e:
            logger.warning(
                "Failed to read OAuth pending state from DynamoDB: %s",
                e,
                extra={"pending_key": session_id},
                exc_info=True,
            )
    entry = _pending_memory.get(session_id)
    if not entry:
        logger.info(
            "OAuth pending state miss",
            extra={
                "pending_key_kind": _pending_key_kind(session_id),
                "pending_key": session_id,
                "storage": "memory",
            },
        )
        return None
    proxy_state, expires_at = entry
    if time.time() > expires_at:
        _pending_memory.pop(session_id, None)
        logger.info(
            "OAuth pending state expired",
            extra={
                "pending_key_kind": _pending_key_kind(session_id),
                "pending_key": session_id,
                "storage": "memory",
            },
        )
        return None
    logger.info(
        "OAuth pending state hit",
        extra={
            "pending_key_kind": _pending_key_kind(session_id),
            "pending_key": session_id,
            "storage": "memory",
            "proxy_state_length": len(proxy_state),
        },
    )
    return proxy_state


def delete_pending_proxy_state(session_id: str) -> None:
    if not session_id:
        return
    _pending_memory.pop(session_id, None)
    if not OAUTH_PENDING_TABLE_NAME:
        return
    try:
        _get_dynamodb_client().delete_item(
            TableName=OAUTH_PENDING_TABLE_NAME,
            Key={"session_id": {"S": session_id}},
        )
    except Exception as e:
        logger.warning(
            "Failed to delete OAuth pending state from DynamoDB: %s",
            e,
            exc_info=True,
        )
