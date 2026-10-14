"""Public admission API (kept separate for business integrations)."""
from .registration import PackageProtection
from .store import ProtectionError
__all__=['PackageProtection','ProtectionError']
