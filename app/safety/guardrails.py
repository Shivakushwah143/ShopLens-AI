from urllib.parse import urlparse


SENSITIVE_PATTERNS = [
    "login",
    "signin",
    "checkout",
    "payment",
    "billing",
    "bank",
    "delete",
    "submit",
]


class SafetyGuard:
    def is_safe_url(self, url: str) -> bool:
        lowered = url.lower()
        return not any(pattern in lowered for pattern in SENSITIVE_PATTERNS)

    def reason_for_block(self, url: str) -> str | None:
        if self.is_safe_url(url):
            return None
        domain = urlparse(url).netloc
        return f"Blocked navigation to potentially sensitive destination: {domain}"


safety_guard = SafetyGuard()
