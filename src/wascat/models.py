"""Imports every mapped class so Base.metadata is complete.

Alembic's autogenerate only sees tables whose modules have been imported, and
a missing import here silently produces a migration that drops tables. Import
this module rather than reaching into the domain packages one by one.
"""

from wascat.core.db import Base
from wascat.domains.audit.models import AuditEvent
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.domains.iam.models import (
    LoginAttempt,
    Permission,
    RefreshSession,
    Role,
    User,
    role_permissions,
    user_roles,
)
from wascat.domains.ingest.models import IngestRun, IngestStatus, SequenceReference
from wascat.domains.vocab.models import VocabKind, VocabularyAlias, VocabularyTerm

__all__ = [
    "Artifact",
    "AuditEvent",
    "Base",
    "Collection",
    "ImageRecord",
    "IngestRun",
    "IngestStatus",
    "LoginAttempt",
    "Permission",
    "RefreshSession",
    "Release",
    "ReleaseStatus",
    "Role",
    "SequenceReference",
    "User",
    "VocabKind",
    "VocabularyAlias",
    "VocabularyTerm",
    "role_permissions",
    "user_roles",
]
