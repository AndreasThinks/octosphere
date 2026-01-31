"""AT Proto client package.

Provides AT Protocol integration using the official atproto SDK with support
for any PDS through identity resolution.
"""

from octosphere.atproto.client import AtprotoAuth, AtprotoClient, CreateRecordResult
from octosphere.atproto.models import (
    OCTOSPHERE_PUBLICATION_NSID,
    OCTOPUS_PUBLICATION_NSID,  # deprecated alias
    OctospherePublicationRecord,
    PublicationType,
)

__all__ = [
    "AtprotoAuth",
    "AtprotoClient",
    "CreateRecordResult",
    "OCTOSPHERE_PUBLICATION_NSID",
    "OCTOPUS_PUBLICATION_NSID",  # deprecated alias
    "OctospherePublicationRecord",
    "PublicationType",
]
