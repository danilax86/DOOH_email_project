import os
import pytest
from playwright.sync_api import Page, expect, Playwright
import requests
from typing import Generator

@pytest.fixture(scope="session")
def base_url() -> str:
    return os.getenv("BASE_URL", "http://localhost:5000")

@pytest.fixture(scope="function")
def page(playwright: Playwright, base_url: str) -> Generator[Page, None, None]:
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            '--disable-dev-shm-usage',
            '--no-sandbox',
            '--disable-setuid-sandbox'
        ]
    )
    context = browser.new_context(
        viewport={'width': 1280, 'height': 1080},
        java_script_enabled=True
    )
    page = context.new_page()
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(30000)
    def log_request(request):
        print(f"Request: {request.method} {request.url}")
    def log_response(response):
        print(f"Response: {response.status} {response.url}")
    page.on("request", log_request)
    page.on("response", log_response)
    try:
        response = page.goto(
            base_url,
            wait_until="networkidle",
            timeout=30000
        )
        print(f"{response.status if response else 'No response'}")
    except Exception as e:
        print(str(e))
        page.screenshot(path="/app/test/test-results/initial_page_load_error.png")
        raise
    yield page
    try:
        context.close()
        browser.close()
    except Exception as e:
        print(str(e))

@pytest.fixture(scope="module")
def test_user() -> dict:
    return {
        "username": "testuser@example.com",
        "password": "testpassword123"
    }

@pytest.fixture(scope="module")
def test_email_data() -> dict:
    return {
        "subject": "Test Email Subject",
        "body": "This is a test email body.",
        "recipients": ["recipient1@example.com", "recipient2@example.com"]
    }

@pytest.fixture(scope="module")
def mailhog_client():
    class MailHogClient:
        def __init__(self, host='mailhog', port=8025):
            self.base_url = f"http://{host}:{port}/api/v2"
        def get_messages(self, limit=50):
            return requests.get(f"{self.base_url}/messages?limit={limit}").json()
        def get_latest_message(self):
            messages = self.get_messages(limit=1)
            if messages and 'items' in messages and messages['items']:
                return messages['items'][0]
            return None
        def delete_all_messages(self):
            return requests.delete(f"{self.base_url}/messages")
        def get_emails(self):
            messages = self.get_messages(limit=100)
            if not messages or 'items' not in messages:
                return []
            email_objects = []
            for item in messages['items']:
                content = item.get('Content', {})
                headers = content.get('Headers', {})
                to_addresses = headers.get('To', [])
                if not to_addresses:
                    to_addresses = []
                elif isinstance(to_addresses, str):
                    to_addresses = [to_addresses]
                subject_list = headers.get('Subject', [])
                subject = subject_list[0] if subject_list else ''
                from_list = headers.get('From', [])
                from_addr = from_list[0] if from_list else ''
                email_obj = type('Email', (), {
                    'to': to_addresses,
                    'subject': subject,
                    'from': from_addr,
                    'body': content.get('Body', ''),
                    'raw': item
                })()
                email_objects.append(email_obj)
            return email_objects
    return MailHogClient(
        host=os.getenv("MAILHOG_HOST", "localhost"),
        port=int(os.getenv("MAILHOG_HTTP_PORT", "8025"))
    )
