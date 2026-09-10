"""Host the Atlassian agent as a Microsoft Teams bot, acting as each sender.

Requires the `teams` extra (Python 3.12+):

    pip install -e ".[teams]"
    python -m atlassian_agent.teams
"""

from .config import TeamsSettings
from .identity import TeamsUser, issue_sign_in_state, user_from_activity, verify_sign_in_state
from .store import ScopedTokenStorage, UserTokenStore, generate_encryption_key

__all__ = [
    "ScopedTokenStorage",
    "TeamsSettings",
    "TeamsUser",
    "UserTokenStore",
    "generate_encryption_key",
    "issue_sign_in_state",
    "user_from_activity",
    "verify_sign_in_state",
]
