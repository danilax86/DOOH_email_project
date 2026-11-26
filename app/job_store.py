import base64
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager

from cryptography.fernet import Fernet


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATABASE_PATH = os.environ.get(
    "JOB_DB_PATH", os.path.join(DATA_DIR, "email_jobs.sqlite3")
)


@contextmanager
def _connection():
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _cipher():
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY must be configured to encrypt queued credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


def seal_secret(value):
    return _cipher().encrypt(value.encode()).decode()


def open_secret(value):
    return _cipher().decrypt(value.encode()).decode()


def initialize_database():
    with _connection() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS email_jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('created', 'processing', 'completed', 'error')),
                message TEXT NOT NULL,
                data TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                batch INTEGER NOT NULL DEFAULT 0,
                total INTEGER,
                error TEXT,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                updated_at REAL NOT NULL DEFAULT (unixepoch())
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS email_jobs_state_created ON email_jobs(state, created_at)"
        )


def create_job(job_id, data):
    initialize_database()
    with _connection() as connection:
        connection.execute(
            "INSERT INTO email_jobs (id, state, message, data) VALUES (?, 'created', ?, ?)",
            (job_id, "Письма созданы, отправка находится в очереди, подождите.", json.dumps(data, ensure_ascii=False)),
        )


def get_job(job_id):
    initialize_database()
    with _connection() as connection:
        row = connection.execute(
            "SELECT id, state, message, sent, batch, total, error FROM email_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["done"] = job["state"] in {"completed", "error"}
    return job


def claim_next_job():
    initialize_database()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, data FROM email_jobs WHERE state = 'created' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            "UPDATE email_jobs SET state = 'processing', message = ?, updated_at = unixepoch() "
            "WHERE id = ? AND state = 'created'",
            ("Задача обрабатывается.", row["id"]),
        )
        if updated.rowcount != 1:
            return None
        return row["id"], json.loads(row["data"])


def update_job(job_id, *, message, sent=None, batch=None, total=None):
    fields = ["message = ?", "updated_at = unixepoch()"]
    values = [message]
    for field, value in (("sent", sent), ("batch", batch), ("total", total)):
        if value is not None:
            fields.append(f"{field} = ?")
            values.append(value)
    values.append(job_id)
    with _connection() as connection:
        connection.execute(
            f"UPDATE email_jobs SET {', '.join(fields)} WHERE id = ? AND state = 'processing'",
            values,
        )


def finish_job(job_id, *, state, message, error=None, sent=None, batch=None, total=None):
    if state not in {"completed", "error"}:
        raise ValueError("Terminal state must be completed or error")
    with _connection() as connection:
        connection.execute(
            """UPDATE email_jobs SET state = ?, message = ?, error = ?,
                sent = COALESCE(?, sent), batch = COALESCE(?, batch),
                total = COALESCE(?, total), data = '{}', updated_at = unixepoch()
                WHERE id = ?""",
            (state, message, error, sent, batch, total, job_id),
        )


def mark_interrupted_jobs():
    initialize_database()
    with _connection() as connection:
        connection.execute(
            """UPDATE email_jobs SET state = 'error',
                message = 'Задача прервана перезапуском фонового обработчика.',
                error = 'Worker restarted while job was processing', data = '{}',
                updated_at = unixepoch()
                WHERE state = 'processing'"""
        )
