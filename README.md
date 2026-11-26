# DOOH_email_project
Mass mailing tool using .xlsx contact list


No longer hosted on: https://dooh-email-project.onrender.com/ 

## Docker

```powershell
docker-compose up --build
```

The app will be available at http://localhost:7860/

Docker Compose starts the web app and one background email worker. Both use a
shared named volume for the SQLite job database and uploaded files. Keep exactly
one `email-worker` service instance running so email jobs are processed by one
consumer.

For local development, start the web app and worker in separate terminals, with
the same `.env` configuration loaded in both:

```powershell
py run.py
py -m app.email_worker
```


