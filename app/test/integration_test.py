# test/integration_test.py

import io
import os
import threading
import time
from uuid import UUID

import pandas as pd
import pytest
from flask import url_for

import run as run_app
from app import email_sender as es
def test_split_emails_various_separators():
    s = "a@a.com, b@b.com; c@c.com / d@d.com | e@e.com и f@f.com"
    parts = es.split_emails(s)

    assert "a@a.com" in parts
    assert "f@f.com" in parts
    assert len(parts) >= 6

# хэлперы

def make_excel_bytes(df: pd.DataFrame) -> bytes:
    """Return Excel bytes for multipart upload."""
    bio = io.BytesIO()
    df.to_excel(bio, index=False)
    bio.seek(0)
    return bio.read()


class DummySMTP:
    """A dummy SMTP server replacement that records messages sent."""

    def __init__(self, host=None, port=None, context=None):
        self.host = host
        self.port = port
        self.context = context
        self.sent_messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        pass

    def set_debuglevel(self, n):
        pass

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if user == "bad@example.com":
            raise Exception("Auth failed")
        return True

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent_messages.append((from_addr, to_addrs, str(msg)))

    def quit(self):
        pass


class SyncThread:
    """Monkeypatch target for threading.Thread that runs target immediately on start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return


# тесты 

@pytest.fixture(autouse=True)
def temp_upload_folder(tmp_path, monkeypatch):
    """Redirect the app's upload folder to a temporary directory during tests."""
    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setattr(run_app.app, "config", {**run_app.app.config, "UPLOAD_FOLDER": str(folder)})
    yield


def test_preview_excel_success(monkeypatch):
    client = run_app.app.test_client()

    df = pd.DataFrame([
        {"email": "alice@example.com", "name": "Alice", "city": "Moscow", "mall": 'Mega', "rim": "RIM1", "num":"1", "size":"M", "link":"http://img", "min":"1", "sec":"10"}
    ])

    b = make_excel_bytes(df)

    data = {
        "contacts_file": (io.BytesIO(b), "contacts.xlsx"),
        "add_tc_prefix": "true"
    }

    resp = client.post("/preview-excel", data=data, content_type='multipart/form-data')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert '<table' in text
    assert 'id="first-row-data"' in text
    assert 'data-mall="' in text and 'data-city="' in text


def test_preview_excel_missing_columns():
    client = run_app.app.test_client()

    df = pd.DataFrame([{"email": "bob@example.com", "name": "Bob"}])
    b = make_excel_bytes(df)

    data = {
        "contacts_file": (io.BytesIO(b), "contacts.xlsx"),
    }

    resp = client.post("/preview-excel", data=data, content_type='multipart/form-data')
    assert resp.status_code == 400
    assert "отсутствуют обязательные столбцы" in resp.get_data(as_text=True) or "обязательных столбцов" in resp.get_data(as_text=True)


def test_preview_excel_empty_email_row():
    client = run_app.app.test_client()

    df = pd.DataFrame([
        {"email": "carol@example.com", "name": "Carol", "mall": "Mall", "city": "Sochi"},
        {"email": "", "name": "Empty", "mall": "Mall", "city": "Sochi"}
    ])
    b = make_excel_bytes(df)

    data = {
        "contacts_file": (io.BytesIO(b), "contacts.xlsx"),
    }

    resp = client.post("/preview-excel", data=data, content_type='multipart/form-data')
    assert resp.status_code == 400
    assert "строки без email" in resp.get_data(as_text=True) or "не содержит email" in resp.get_data(as_text=True)


def test_get_contacts_from_excel_and_grouping(tmp_path):
    df = pd.DataFrame([
        {"email": "a@example.com", "name": "A", "mall": "Mall1", "city": "Msk", "rim": "R1", "num": "1", "size": "S", "link": "L", "min": "1", "sec": "5"},
        {"email": "a@example.com", "name": "A", "mall": "Mall2", "city": "Msk", "rim": "R2", "num": "2", "size": "M", "link": "L2", "min": "2", "sec": "10"},
        {"email": "b@example.com", "name": "", "mall": "Mall1", "city": "Spb", "rim": "RB", "num": "1", "size": "L", "link": "", "min": "", "sec": ""}
    ])
    fp = tmp_path / "contacts.xlsx"
    df.to_excel(fp, index=False)

    contacts = es.get_contacts_from_excel(str(fp), template_text=None, add_prefix=True)
    emails = sorted([c['email'] for c in contacts])
    assert emails == ["a@example.com", "b@example.com"]
    for c in contacts:
        if c['email'] == 'b@example.com':
            assert c['name'] == 'Коллеги' or c['name'] != ''
    for c in contacts:
        if c['email'] == 'a@example.com':
            assert c['mall_count'] >= 2 or c['mall'] != ''


def test_send_batch_uses_smtp(monkeypatch):
    dummy_server = DummySMTP()

    def fake_smtp_ssl(host, port, context=None):
        return dummy_server

    monkeypatch.setattr(es, "SMTP_HOST", "dummy-host")
    monkeypatch.setattr(es, "SMTP_PORT", 123)
    monkeypatch.setattr(es, "SMTP_PROTOCOL", "SSL")
    monkeypatch.setattr("smtplib.SMTP_SSL", fake_smtp_ssl)

    template_text = "Hello ${NAME}"
    contacts = [
        {"email": "x@example.com", "name": "X", "mall_count": 1, "mall": "T", "city": "Msk", "_cc_emails": []},
        {"email": "y@example.com", "name": "Y", "mall_count": 1, "mall": "T2", "city": "Msk", "_cc_emails": []}
    ]

    sent = es.send_batch(
        my_address="me@example.com",
        password="pw",
        batch_contacts=contacts,
        cc_addresses=[],
        brand="Brand",
        period="P",
        doc="",
        template_text=template_text,
        display_name="Me"
    )
    assert sent == len(contacts)
    assert len(dummy_server.sent_messages) == len(contacts)


def test_send_emails_endpoint_flow(monkeypatch, tmp_path):
    client = run_app.app.test_client()

    df = pd.DataFrame([{"email": "alice@example.com", "name": "Alice", "mall": "Mall", "city": "Msk"}])
    excel_bytes = make_excel_bytes(df)

    # фейковые параметры сессии
    with client.session_transaction() as sess:
        sess['MY_ADDRESS'] = 'me@example.com'
        sess['PASSWORD'] = 'pw'
        sess['DISPLAY_NAME'] = 'Me'

    fake_contacts = [
        {"email": "a1@example.com", "name": "A1", "city": "Msk", "rim": "", "mall": "M1", "mall_count": 1, "_cc_emails": []},
        {"email": "a2@example.com", "name": "A2", "city": "Msk", "rim": "", "mall": "M2", "mall_count": 1, "_cc_emails": []},
        {"email": "a3@example.com", "name": "A3", "city": "Msk", "rim": "", "mall": "M3", "mall_count": 1, "_cc_emails": []},
    ]

    monkeypatch.setattr(run_app, "get_contacts_from_excel", lambda path, **kwargs: fake_contacts)
    monkeypatch.setattr(es, "get_contacts_from_excel", lambda path, **kwargs: fake_contacts)

    def fake_send_batch(**kwargs):
        return len(kwargs.get('batch_contacts', []))

    monkeypatch.setattr(es, "send_batch", fake_send_batch)

    monkeypatch.setattr(threading, "Thread", SyncThread)

    data = {
        "contacts_file": (io.BytesIO(excel_bytes), "contacts.xlsx"),
        "message_template": "Hello ${NAME}",
        "batch_size": "2", 
        "pause_seconds": "0"
    }

    resp = client.post("/send-emails", data=data, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Письма отправляются" in text or "Письма" in text

    assert run_app.app.jobs, "No jobs created"

    job_id = next(iter(run_app.app.jobs.keys()))
    job = run_app.app.jobs[job_id]

    assert job['done'] is True
    assert job['sent'] == len(fake_contacts)
    assert "Письма успешно отправлены" in job['status'] or "успешно" in job['status']


def test_split_emails_various_separators():
    s = "a@example.com, b@example.com; c@example.com / d@example.com | e@example.com и f@example.com"
    parts = es.split_emails(s)
    assert "a@example.com" in parts
    assert "f@example.com" in parts
    assert len(parts) >= 6
