"""Adaptive cards the bot sends."""

from __future__ import annotations

from typing import Any


def sign_in_card(display_name: str, sign_in_url: str) -> dict[str, Any]:
    """Prompt one person to connect their own Atlassian account.

    The link is bound to a single Teams identity, so it names who it is for -
    anyone else who ends up with it should not complete it.
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Connect your Atlassian account",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "text": (
                    f"**{display_name}**, I act on Jira and Confluence using *your* "
                    "Atlassian permissions, so you need to connect your own account first."
                ),
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
                "text": (
                    "This link is for you alone and works once, for a few minutes. "
                    "Do not let anyone else open it - they would link *their* Atlassian "
                    "account to *your* Teams identity."
                ),
            },
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Connect Atlassian", "url": sign_in_url}],
    }
