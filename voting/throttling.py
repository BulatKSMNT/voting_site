from rest_framework.throttling import SimpleRateThrottle
from .permissions import identity


class ActorThrottle(SimpleRateThrottle):
    scope = "read"

    def __init__(self):
        # Scope depends on the endpoint, chosen in allow_request.
        pass

    def allow_request(self, request, view):
        self.scope = "read" if request.method in {"GET", "HEAD", "OPTIONS"} else "vote" if view.basename == "vote" else "manage"
        if not request.user.is_authenticated:
            self.scope = "anon"
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        actor = identity(request)
        if actor["source"] == "bot" and actor["actor_id"]:
            ident = f"telegram:{actor['actor_id']}"
        elif request.user.is_authenticated:
            ident = f"user:{request.user.pk}"
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
