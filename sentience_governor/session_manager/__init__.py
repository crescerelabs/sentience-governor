"""Session Manager — four-state lifecycle machine keyed by session_id."""

from sentience_governor.session_manager.manager import SessionManager, SessionState

__all__ = ["SessionManager", "SessionState"]
