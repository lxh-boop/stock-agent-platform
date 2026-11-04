from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools.user_profile_tool import query_user_profile


class UserProfileService:
    """Read-only service for Agent-facing user profile access."""

    def get_user_profile(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        result = query_user_profile(
            user_id,
            db_path=db_path,
            output_dir=output_dir,
        )
        data = dict(result or {})
        return {
            "success": str(data.get("status") or "").lower() == "success",
            "message": "User profile loaded.",
            "data": {
                **data,
                "read_only": True,
                "mutation_performed": False,
            },
            "warnings": [],
            "errors": [],
            "tool_name": "user.profile.get",
        }


user_profile_service = UserProfileService()
