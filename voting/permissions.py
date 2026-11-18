import secrets
from django.conf import settings
from rest_framework.permissions import BasePermission
from core.observability import context


def identity(request):
    key = getattr(request.auth, "key", "")
    configured = settings.DJANGO_API_TOKEN
    if configured and key and secrets.compare_digest(key, configured):
        raw = request.headers.get("X-Telegram-User-ID", "")
        actor = int(raw) if len(raw) <= 19 and raw.isdigit() and 0 < int(raw) < 2**63 else None
        role = "full" if actor in settings.FULL_ADMIN_IDS else "limited" if actor in settings.LIMITED_ADMIN_IDS else "viewer"
        return {"source": "bot", "actor_id": actor, "actor_role": role}
    if request.user.is_authenticated:
        return {"source": "api", "actor_id": request.user.pk,
                "actor_role": "full" if request.user.is_staff else "viewer"}
    return {"source": "anonymous", "actor_id": None, "actor_role": "viewer"}


def bind_identity(request):
    actor = identity(request)
    context.set({**context.get(), **actor})
    return actor


class VotingPermission(BasePermission):
    message = "Недостаточно прав для этой операции."

    def has_permission(self, request, view):
        actor = bind_identity(request)
        if not request.user.is_authenticated or (actor["source"] == "bot" and actor["actor_id"] is None):
            return False
        if view.basename == 'campaign' and view.action == 'my_access':
            return actor['source'] == 'bot'
        if view.basename == "vote":
            return actor["source"] == "bot" or actor["actor_role"] == "full"
        if actor["actor_role"] == "full":
            return True
        if actor["actor_role"] != "limited":
            return False
        if view.basename == "round":
            if view.action in {"list", "retrieve", "end_and_transfer", "event_start", "event_finish", "event_results"}:
                return True
            if view.action in {"update", "partial_update"}:
                return set(request.data) <= {"status", "is_current"}
        if view.basename == 'campaign' and view.action == 'event':
            return True
        return False

    def has_object_permission(self, request, view, obj):
        # Reopening an archived round is the full-admin /resurrect_round command.
        if identity(request)["actor_role"] == "limited" and getattr(obj, "status", None) == "ended":
            return request.method in {"GET", "HEAD", "OPTIONS"}
        return True
