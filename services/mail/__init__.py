"""Isolated mail research domain for SJFX.

This package deliberately owns only ``mail_*`` tables and ``/api/mail``
routes.  It does not participate in the legacy document ingestion pipeline.
"""

from .bootstrap import MailServices, register_mail_module

__all__ = ["MailServices", "register_mail_module"]
