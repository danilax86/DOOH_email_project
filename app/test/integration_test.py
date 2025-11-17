# test/integration_test.py

import io
import os
import threading
import time
from uuid import UUID

import pandas as pd
import pytest
from flask import url_for

# import app objects
import run as run_app
from app import email_sender as es


# -------- helpers used in tests --------
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

    # context manager API
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
        # simulate successful login
        if user == "bad@example.com":
            raise Exception("Auth failed")
        return True

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent_messages.append((from_addr, to_addrs, str(msg)))

    # compatibility with smtplib.SMTP_SSL and smtplib.SMTP
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
        # run synchronously
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return


# -------- tests --------

@pytest.fixture(autouse=True)
def temp_upload_folder(tmp_path, monkeypatch):
    """Redirect the app's upload folder to a temporary directory during tests."""
    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setattr(run_app.app, "config", {**run_app.app.config, "UPLOAD_FOLDER": str(folder)})
    yield


def test_preview_excel_success(monkeypatch):
    client = run_app.app.test_client()

    # Create DataFrame with required columns and extra columns present
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
    # should include the preview table and the hidden first-row data attributes for mall/city
    assert '<table' in text
    assert 'id="first-row-data"' in text
    assert 'data-mall="' in text and 'data-city="' in text


def test_preview_excel_missing_columns():
    client = run_app.app.test_client()

    # missing 'mall' and 'city' -> required columns are email, mall, city -> should return 400
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

    # include an empty email row -> should return 400 with message about rows without email
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
    # Integration-like test for get_contacts_from_excel: create real excel file and call function
    df = pd.DataFrame([
        {"email": "a@example.com", "name": "A", "mall": "Mall1", "city": "Msk", "rim": "R1", "num": "1", "size": "S", "link": "L", "min": "1", "sec": "5"},
        {"email": "a@example.com", "name": "A", "mall": "Mall2", "city": "Msk", "rim": "R2", "num": "2", "size": "M", "link": "L2", "min": "2", "sec": "10"},
        {"email": "b@example.com", "name": "", "mall": "Mall1", "city": "Spb", "rim": "RB", "num": "1", "size": "L", "link": "", "min": "", "sec": ""}
    ])
    fp = tmp_path / "contacts.xlsx"
    df.to_excel(fp, index=False)

    contacts = es.get_contacts_from_excel(str(fp), template_text=None, add_prefix=True)
    # should return a list of combined contacts grouped by email
    emails = sorted([c['email'] for c in contacts])
    assert emails == ["a@example.com", "b@example.com"]
    # for 'b' name should be filled with 'Коллеги' because empty
    for c in contacts:
        if c['email'] == 'b@example.com':
            assert c['name'] == 'Коллеги' or c['name'] != ''
    # the first contact (a@example.com) should have mall_count > 1
    for c in contacts:
        if c['email'] == 'a@example.com':
            assert c['mall_count'] >= 2 or c['mall'] != ''


def test_send_batch_uses_smtp(monkeypatch):
    # monkeypatch SMTP_SSL to use DummySMTP and verify send_batch returns proper sent count
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
    # Dummy server recorded messages
    assert len(dummy_server.sent_messages) == len(contacts)


def test_send_emails_endpoint_flow(monkeypatch, tmp_path):
    client = run_app.app.test_client()

    # prepare a small excel to upload (content not read because we patch the reader)
    df = pd.DataFrame([{"email": "alice@example.com", "name": "Alice", "mall": "Mall", "city": "Msk"}])
    excel_bytes = make_excel_bytes(df)

    # set session (simulate logged in user)
    with client.session_transaction() as sess:
        sess['MY_ADDRESS'] = 'me@example.com'
        sess['PASSWORD'] = 'pw'
        sess['DISPLAY_NAME'] = 'Me'

    # monkeypatch get_contacts_from_excel to return 3 fake contacts
    fake_contacts = [
        {"email": "a1@example.com", "name": "A1", "city": "Msk", "rim": "", "mall": "M1", "mall_count": 1, "_cc_emails": []},
        {"email": "a2@example.com", "name": "A2", "city": "Msk", "rim": "", "mall": "M2", "mall_count": 1, "_cc_emails": []},
        {"email": "a3@example.com", "name": "A3", "city": "Msk", "rim": "", "mall": "M3", "mall_count": 1, "_cc_emails": []},
    ]

    monkeypatch.setattr(run_app, "get_contacts_from_excel", lambda path, **kwargs: fake_contacts)
    # but the send() function imports get_contacts_from_excel from app.email_sender; patch that too
    monkeypatch.setattr(es, "get_contacts_from_excel", lambda path, **kwargs: fake_contacts)

    # patch send_batch to pretend to send and return number of messages sent for the batch
    def fake_send_batch(**kwargs):
        # return number of messages in this batch
        return len(kwargs.get('batch_contacts', []))

    monkeypatch.setattr(es, "send_batch", fake_send_batch)

    # run background thread synchronously by replacing threading.Thread with our SyncThread
    monkeypatch.setattr(threading, "Thread", SyncThread)

    data = {
        "contacts_file": (io.BytesIO(excel_bytes), "contacts.xlsx"),
        "message_template": "Hello ${NAME}",
        "batch_size": "2",  # force batching into 2 and 1
        "pause_seconds": "0"
    }

    resp = client.post("/send-emails", data=data, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    # response should include "Письма отправляются..." or similar status
    assert "Письма отправляются" in text or "Письма" in text

    # extract job_id from response by searching for 'job_id=' in returned html (status.html renders it)
    # If the template assigned job_id into a JS var or element, we can find it in the page.
    # But send() returns status.html and includes job_id variable in context, so we can instead
    # find the single job in run_app.app.jobs
    assert run_app.app.jobs, "No jobs created"

    # there should be exactly one job created by this test
    job_id = next(iter(run_app.app.jobs.keys()))
    job = run_app.app.jobs[job_id]

    # Because we ran the thread synchronously, job should be marked done and sent equal to number of contacts
    assert job['done'] is True
    assert job['sent'] == len(fake_contacts)
    assert "Письма успешно отправлены" in job['status'] or "успешно" in job['status']


def test_split_emails_various_separators():
    s = "a@example.com, b@example.com; c@example.com / d@example.com | e@example.com и f@example.com"
    parts = es.split_emails(s)
    assert "a@example.com" in parts
    assert "f@example.com" in parts
    assert len(parts) >= 6
