"""Push notification support for agent-mail messages.

This module provides mechanisms to notify receiving agents when messages arrive,
supporting both signal files (for local deployments) and webhook callbacks.

Signal files are created in a configurable directory when messages are sent,
allowing receiving agents' hooks to detect and inject pending messages.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import logging

import httpx

logger = logging.getLogger(__name__)


def _get_signal_dir() -> Path | None:
    """Get the signal directory from environment or default location."""
    signal_dir = os.environ.get("AGENT_MAIL_SIGNAL_DIR")
    if signal_dir:
        return Path(signal_dir)

    # Default: ~/.claude/agent-mail-signals/ (for Claude Code integration)
    home = Path.home()
    default_dir = home / ".claude" / "agent-mail-signals"
    if default_dir.exists() or os.environ.get("AGENT_MAIL_SIGNALS_ENABLED", "").lower() in ("1", "true", "yes"):
        return default_dir

    return None


def create_signal_file(
    recipient_name: str,
    sender_name: str,
    subject: str,
    message_id: int,
    project_key: str,
    signal_dir: Path | None = None,
) -> Path | None:
    """
    Create a signal file to notify a recipient of a new message.

    Signal files are JSON lines appended to {signal_dir}/{recipient_name}.signal

    Args:
        recipient_name: The recipient agent's name
        sender_name: The sender agent's name
        subject: Message subject line
        message_id: Database message ID
        project_key: Project identifier
        signal_dir: Override signal directory (defaults to env/config)

    Returns:
        Path to signal file if created, None if signals are disabled
    """
    if signal_dir is None:
        signal_dir = _get_signal_dir()

    if signal_dir is None:
        return None

    # Ensure directory exists
    signal_dir.mkdir(parents=True, exist_ok=True)

    signal_file = signal_dir / f"{recipient_name}.signal"
    timestamp = datetime.now(timezone.utc).isoformat()

    signal_data = {
        "from": sender_name,
        "to": recipient_name,
        "subject": subject,
        "message_id": message_id,
        "project": project_key,
        "timestamp": timestamp,
    }

    # Append to signal file (multiple messages can queue)
    with open(signal_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(signal_data) + "\n")

    logger.info(f"Created signal for {recipient_name}: {subject} (from {sender_name})")
    return signal_file


async def send_webhook_notification(
    webhook_url: str,
    recipient_name: str,
    sender_name: str,
    subject: str,
    message_id: int,
    project_key: str,
    timeout: float = 5.0,
) -> bool:
    """
    Send a webhook notification for a new message.

    Args:
        webhook_url: URL to POST notification to
        recipient_name: The recipient agent's name
        sender_name: The sender agent's name
        subject: Message subject line
        message_id: Database message ID
        project_key: Project identifier
        timeout: Request timeout in seconds

    Returns:
        True if webhook was successfully delivered, False otherwise
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    payload = {
        "event": "message_received",
        "recipient": recipient_name,
        "sender": sender_name,
        "subject": subject,
        "message_id": message_id,
        "project": project_key,
        "timestamp": timestamp,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            logger.info(f"Webhook notification sent to {webhook_url} for {recipient_name}")
            return True
    except httpx.HTTPError as e:
        logger.warning(f"Webhook notification failed for {recipient_name}: {e}")
        return False


async def notify_recipients(
    recipients: list[dict[str, Any]],
    sender_name: str,
    subject: str,
    message_id: int,
    project_key: str,
    enable_signals: bool = True,
    webhook_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Notify all recipients of a new message via configured channels.

    Args:
        recipients: List of recipient dicts with at least 'name' key
        sender_name: The sender agent's name
        subject: Message subject line
        message_id: Database message ID
        project_key: Project identifier
        enable_signals: Whether to create signal files
        webhook_urls: Optional mapping of agent names to webhook URLs

    Returns:
        Summary of notifications sent
    """
    results = {
        "signals_created": [],
        "webhooks_sent": [],
        "webhooks_failed": [],
    }

    signal_dir = _get_signal_dir() if enable_signals else None
    webhook_urls = webhook_urls or {}

    for recipient in recipients:
        name = recipient.get("name", recipient) if isinstance(recipient, dict) else recipient

        # Create signal file
        if signal_dir is not None:
            signal_path = create_signal_file(
                recipient_name=name,
                sender_name=sender_name,
                subject=subject,
                message_id=message_id,
                project_key=project_key,
                signal_dir=signal_dir,
            )
            if signal_path:
                results["signals_created"].append(name)

        # Send webhook if configured
        if name in webhook_urls:
            success = await send_webhook_notification(
                webhook_url=webhook_urls[name],
                recipient_name=name,
                sender_name=sender_name,
                subject=subject,
                message_id=message_id,
                project_key=project_key,
            )
            if success:
                results["webhooks_sent"].append(name)
            else:
                results["webhooks_failed"].append(name)

    return results
