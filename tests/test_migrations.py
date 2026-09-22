"""The migration graph itself, checked before anything tries to run it.

These need no database. They read the revision files, which is the point: the
failure they exist to catch is one that every branch involved looks innocent
of, and that only appears once two of them are merged.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


def script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


class TestTheGraphHasOneHead:
    """Two heads take the whole service down, and no single branch shows it.

    The container's command is `alembic upgrade head && uvicorn ...`. Give
    alembic two heads and it refuses to choose, the command fails, and the
    server never starts - so this is not a migration problem that surfaces as
    a broken migration, it is one that surfaces as an outage.

    It happened on 2026-09-22: two pull requests, merged a minute apart, each
    chained a new revision to 4f2b8c7d1e05. Both branches had one head and
    were green. The second head existed only in the merge of the two, where
    nothing was looking.

    So the assertion lives here, where it runs on every branch and on the
    merge, rather than being something a reviewer has to notice.
    """

    def test_there_is_exactly_one(self) -> None:
        heads = script_directory().get_heads()
        assert len(heads) == 1, (
            f"{len(heads)} migration heads: {', '.join(sorted(heads))}. "
            f"`alembic upgrade head` cannot choose between them and will fail, "
            f"taking the service down with it. Re-chain the newer revision's "
            f"down_revision onto the other so the history is linear."
        )

    def test_every_revision_is_reachable_from_it(self) -> None:
        """No orphan chains hanging off a revision nobody points at."""
        script = script_directory()
        (head,) = script.get_heads()
        reachable = {revision.revision for revision in script.walk_revisions("base", head)}
        every = {revision.revision for revision in script.walk_revisions()}
        assert every == reachable, (
            f"unreachable revisions: {', '.join(sorted(every - reachable))}. "
            f"They will never be applied by `alembic upgrade head`."
        )


class TestEveryRevisionCanBeUndone:
    def test_each_defines_a_downgrade(self) -> None:
        """A revision with no `downgrade` makes the whole history one-way.

        An empty body is fine and sometimes right - d3b1c7a49f20 deliberately
        does not reinstate the fabricated provenance it cleared. What is not
        fine is the function missing entirely, which is what alembic's
        template leaves behind when a revision is written by hand.
        """
        missing = [
            revision.revision
            for revision in script_directory().walk_revisions()
            if not hasattr(revision.module, "downgrade")
        ]
        assert not missing, f"revisions with no downgrade(): {', '.join(sorted(missing))}"
