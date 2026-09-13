"""Mail-domain dependency assembly and isolated route registration."""

from __future__ import annotations

from dataclasses import dataclass

from config import Config

from .policy import MailPolicy
from .repository import MailRepository


@dataclass(frozen=True)
class MailServices:
    repository: MailRepository
    policy: MailPolicy
    parser: object | None
    translation_service: object | None
    owner_resolver: object | None
    job_factory: object | None
    model: object | None
    storage: object


def build_mail_services(storage, *, parser=None, translation_service=None,
                        owner_resolver=None, job_factory=None, model=None):
    runtime_storage = storage.initialize() if hasattr(storage, "initialize") else storage
    return MailServices(
        repository=MailRepository(runtime_storage),
        policy=MailPolicy.from_config(Config),
        parser=parser,
        translation_service=translation_service,
        owner_resolver=owner_resolver,
        job_factory=job_factory,
        model=model,
        storage=runtime_storage,
    )


def register_mail_module(app, storage, *, parser=None, translation_service=None,
                         owner_resolver=None, job_factory=None, model=None):
    """Install only the mail domain.  Repeated startup calls are idempotent."""
    if not getattr(Config,"MAIL_FEATURE_ENABLED",False):
        return None
    extensions = getattr(app, "extensions", None)
    if extensions is None:
        extensions = {}
        setattr(app, "extensions", extensions)
    existing = extensions.get("sjfx_mail")
    if existing:
        return existing
    services = build_mail_services(
        storage,
        parser=parser,
        translation_service=translation_service,
        owner_resolver=owner_resolver,
        job_factory=job_factory,
        model=model,
    )
    services.repository.migrate()
    extensions["sjfx_mail"] = services
    from .api import register_mail_routes
    register_mail_routes(app, services)
    return services


def worker_mail_services(storage, *, parser=None, translation_service=None, model=None):
    """Build Worker dependencies without registering any HTTP routes."""
    services = build_mail_services(
        storage, parser=parser, translation_service=translation_service, model=model
    )
    services.repository.migrate()
    return services
