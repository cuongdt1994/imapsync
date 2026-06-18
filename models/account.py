"""
Account model — CRUD operations for IMAP accounts.
"""

from datetime import datetime, timezone

from .database import get_connection


def list_accounts(role: str | None = None) -> list[dict]:
    """List all active accounts, optionally filtered by role."""
    conn = get_connection()
    if role:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE active = 1 AND role = ? ORDER BY name",
            (role,),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE active = 1 ORDER BY role, name"
        )
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    """Get a single account by ID."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE id = ? AND active = 1",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def create_account(
    name: str,
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,  # already encrypted
    provider: str = "generic",
    role: str = "source",
    notes: str = "",
) -> int:
    """Create a new account. Returns the new account ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO accounts (name, imap_host, imap_port, username, password,
                              provider, role, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, imap_host, imap_port, username, password,
         provider, role, notes, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_account(
    account_id: int,
    name: str,
    imap_host: str,
    imap_port: int,
    username: str,
    password: str | None,  # None = don't change
    provider: str = "generic",
    role: str = "source",
    notes: str = "",
) -> bool:
    """Update an existing account. Returns True if row was updated."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()

    if password is not None:
        conn.execute(
            """
            UPDATE accounts
            SET name = ?, imap_host = ?, imap_port = ?, username = ?,
                password = ?, provider = ?, role = ?, notes = ?, updated_at = ?
            WHERE id = ? AND active = 1
            """,
            (name, imap_host, imap_port, username, password,
             provider, role, notes, now, account_id),
        )
    else:
        conn.execute(
            """
            UPDATE accounts
            SET name = ?, imap_host = ?, imap_port = ?, username = ?,
                provider = ?, role = ?, notes = ?, updated_at = ?
            WHERE id = ? AND active = 1
            """,
            (name, imap_host, imap_port, username,
             provider, role, notes, now, account_id),
        )
    conn.commit()
    return conn.total_changes > 0


def delete_account(account_id: int) -> bool:
    """Soft-delete an account (set active = 0)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET active = 0, updated_at = ? WHERE id = ?",
        (now, account_id),
    )
    conn.commit()
    return conn.total_changes > 0


def get_accounts_by_ids(ids: list[int]) -> list[dict]:
    """Get multiple accounts by their IDs."""
    if not ids:
        return []
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM accounts WHERE id IN ({placeholders}) AND active = 1",
        ids,
    )
    return [dict(r) for r in rows]
