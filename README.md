# DOOH_email_project
Mass mailing tool using .xlsx contact list


No longer hosted on: https://dooh-email-project.onrender.com/

## Docker

```powershell
docker-compose up --build
```

The app will be available at http://localhost:7860/

### Recommended production env vars

Set a stable secret key before starting in production:

```bash
export FLASK_SECRET_KEY="your-long-random-secret"
```

Optional SMTP runtime controls:

- `SMTP_TIMEOUT_SECONDS` (default: `30`)
- `SMTP_DEBUG_LEVEL` (default: `0`)
