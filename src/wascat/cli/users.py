"""Administrator accounts.

Creating the first account has to happen outside the dashboard, because the
dashboard needs one to sign in. Everything after that belongs in the UI.
"""

from __future__ import annotations

import asyncio
import secrets

import typer
from sqlalchemy import select

from wascat.core.db import dispose_engine, get_sessionmaker
from wascat.domains.iam import service
from wascat.domains.iam.models import Role, User

users_app = typer.Typer(no_args_is_help=True)

EMAIL_ARG = typer.Argument(..., help="Sign-in address.")
NAME_OPTION = typer.Option(None, "--name", help="Display name.")
ROLE_OPTION = typer.Option("admin", "--role", help="Role slug: admin, curator or viewer.")
PASSWORD_OPTION = typer.Option(
    None,
    "--password",
    help="Leave unset to have one generated and printed once.",
)


@users_app.command("create")
def create(
    email: str = EMAIL_ARG,
    name: str | None = NAME_OPTION,
    role: str = ROLE_OPTION,
    password: str | None = PASSWORD_OPTION,
) -> None:
    """Create an account."""
    generated = password is None
    secret = password or secrets.token_urlsafe(18)
    asyncio.run(_create(email=email, name=name, role=role, password=secret))

    typer.secho(f"Created {email} with role '{role}'.", fg=typer.colors.GREEN)
    if generated:
        typer.echo()
        typer.secho(f"  Password: {secret}", fg=typer.colors.YELLOW, bold=True)
        typer.echo("  This is shown once. Store it somewhere safe.")


async def _create(*, email: str, name: str | None, role: str, password: str) -> None:
    try:
        async with get_sessionmaker()() as session:
            existing = await service.get_user_by_email(session, email)
            if existing is not None:
                typer.secho(f"{email} already exists.", fg=typer.colors.RED)
                raise typer.Exit(1)

            known = (await session.execute(select(Role.slug))).scalars().all()
            if role not in known:
                typer.secho(
                    f"No such role '{role}'. Available: {', '.join(sorted(known))}",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)

            await service.create_user(
                session, email=email, password=password, full_name=name, role_slugs=[role]
            )
            await session.commit()
    finally:
        await dispose_engine()


@users_app.command("list")
def list_users() -> None:
    """List accounts and their roles."""
    asyncio.run(_list())


async def _list() -> None:
    try:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(User).order_by(User.email))).scalars().all()
            if not rows:
                typer.echo("No accounts yet. Create one with: wascat users create <email>")
                return
            for user in rows:
                roles = ", ".join(sorted(role.slug for role in user.roles)) or "no roles"
                state = "" if user.is_active else "  [disabled]"
                typer.echo(f"  {user.email:<36} {roles}{state}")
    finally:
        await dispose_engine()


@users_app.command("passwd")
def set_password(email: str = EMAIL_ARG, password: str | None = PASSWORD_OPTION) -> None:
    """Set a password, ending every existing session for that account."""
    generated = password is None
    secret = password or secrets.token_urlsafe(18)
    asyncio.run(_set_password(email=email, password=secret))

    typer.secho(
        f"Password updated for {email}; all their sessions were ended.", fg=typer.colors.GREEN
    )
    if generated:
        typer.secho(f"\n  Password: {secret}", fg=typer.colors.YELLOW, bold=True)


async def _set_password(*, email: str, password: str) -> None:
    try:
        async with get_sessionmaker()() as session:
            user = await service.get_user_by_email(session, email)
            if user is None:
                typer.secho(f"No account for {email}.", fg=typer.colors.RED)
                raise typer.Exit(1)
            await service.set_password(session, user=user, password=password)
            await session.commit()
    finally:
        await dispose_engine()


@users_app.command("grant")
def grant(email: str = EMAIL_ARG, role: str = ROLE_OPTION) -> None:
    """Give an account a role.

    Also the repair for a role that was recreated - stepping the seed migration
    back and forward gives the roles new ids, which drops existing assignments.
    """
    asyncio.run(_grant(email=email, role=role))
    typer.secho(f"{email} now has the '{role}' role.", fg=typer.colors.GREEN)


async def _grant(*, email: str, role: str) -> None:
    try:
        async with get_sessionmaker()() as session:
            user = await service.get_user_by_email(session, email)
            if user is None:
                typer.secho(f"No account for {email}.", fg=typer.colors.RED)
                raise typer.Exit(1)

            found = (await session.execute(select(Role).where(Role.slug == role))).scalars().first()
            if found is None:
                known = (await session.execute(select(Role.slug))).scalars().all()
                typer.secho(
                    f"No such role '{role}'. Available: {', '.join(sorted(known))}",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)

            if all(existing.id != found.id for existing in user.roles):
                user.roles.append(found)
            await session.commit()
    finally:
        await dispose_engine()
