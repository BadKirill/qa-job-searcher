from fastapi import Request
from sqlalchemy import func, select

from qa_job_bot import pg_schema as s
from qa_job_bot.account_routes import User
from qa_job_bot.runtime_limits import RuntimeLimits


def workspace_summary(store):
    with store.read() as connection:
        preferences = store.preferences_in(connection)
        template = connection.execute(
            select(s.templates.c.payload).where(
                store.owned(s.templates), s.templates.c.status == "active"
            )
        ).scalar_one_or_none()
        counts = dict(
            connection.execute(
                select(s.tasks.c.status, func.count())
                .where(store.owned(s.tasks), s.tasks.c.status != "deleted")
                .group_by(s.tasks.c.status)
            ).all()
        )
        enabled = [source for source in preferences["sources"]["items"] if source["enabled"]]
    return {
        "setup": {
            "search_configured": preferences["search_profile"]["configured"],
            "profile_version": preferences["search_profile"]["version"],
            "enabled_sources": len(enabled),
            "valid_sources": sum(source["valid"] for source in enabled),
            "sources_version": preferences["sources"]["version"],
            "template_ready": template is not None,
            "template_name": template.get("filename", "CV") if template else None,
        },
        "queue": {
            "queued": counts.get("queued", 0),
            "active": sum(
                counts.get(status, 0) for status in ["running", "verifying", "rendering"]
            ),
            "attention": sum(
                counts.get(status, 0) for status in ["needs_input", "blocked", "failed"]
            ),
        },
        "usage": RuntimeLimits(store.database).summary(store.user_id),
    }


def register_workspace_routes(app):
    @app.get("/api/workspace")
    def workspace(request: Request, user: User):
        return workspace_summary(request.state.store)
