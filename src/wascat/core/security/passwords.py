"""Password hashing.

Argon2id via pwdlib. Chosen over passlib because passlib has had no release
since 2020 and its bcrypt backend breaks against bcrypt 4.1+; FastAPI's own
documentation moved to pwdlib for the same reason.

Hashing is deliberately slow, so every call goes through a worker thread. On
the event loop it would stall every other request for the duration.
"""

from __future__ import annotations

from anyio import to_thread
from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Check a password, and report a rehash when the parameters have moved on.

    Returns ``(valid, updated_hash)``. ``updated_hash`` is not None when the
    stored hash used older parameters and should be replaced, which lets
    accounts migrate to stronger settings as people sign in.
    """
    return await to_thread.run_sync(_hasher.verify_and_update, password, password_hash)


def hash_password_sync(password: str) -> str:
    """For the CLI, which has no event loop to protect."""
    return _hasher.hash(password)
