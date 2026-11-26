import json
import math
import os
import time

from dotenv import load_dotenv

load_dotenv()

from app.email_sender import get_contacts_from_excel, pluralize, send_batch
from app.job_store import (
    claim_next_job,
    finish_job,
    mark_interrupted_jobs,
    open_secret,
    update_job,
)


def process_job(job_id, data):
    file_path = data["file_path"]
    sent_total = 0
    batch_number = 0
    total_batches = 0
    try:
        contacts = get_contacts_from_excel(
            file_path,
            template_text=data["template_text"],
            doc=data["doc"],
            add_prefix=data["add_prefix"],
        )
        total_batches = math.ceil(len(contacts) / data["batch_size"]) if contacts else 0
        update_job(job_id, message="Подготовка писем.", total=total_batches)

        for batch_number in range(total_batches):
            start = batch_number * data["batch_size"]
            batch_contacts = contacts[start : start + data["batch_size"]]
            sent_total += send_batch(
                my_address=data["my_address"],
                password=open_secret(data["password"]),
                batch_contacts=batch_contacts,
                cc_addresses=data["cc_addresses"],
                brand=data["brand"],
                period=data["period"],
                doc=data["doc"],
                template_text=data["template_text"],
                display_name=data["display_name"],
            )
            update_job(
                job_id,
                message=f"Отправлено {batch_number + 1}/{total_batches} партий. Через таймаут отправка продолжится.",
                sent=sent_total,
                batch=batch_number + 1,
                total=total_batches,
            )
            if batch_number + 1 < total_batches:
                time.sleep(data["pause_seconds"])

        count = len(contacts)
        word = pluralize(count, ("адрес", "адреса", "адресов"))
        finish_job(
            job_id,
            state="completed",
            message=f"✅ Письма успешно отправлены на {count} {word}.",
            sent=sent_total,
            batch=batch_number + 1 if total_batches else 0,
            total=total_batches,
        )
    except Exception as error:
        finish_job(
            job_id,
            state="error",
            message=f"❌ Ошибка при отправке: {error}",
            error=str(error),
            sent=sent_total,
            batch=batch_number,
            total=total_batches,
        )
    finally:
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass


def main():
    mark_interrupted_jobs()
    print("Email worker started; waiting for jobs.", flush=True)
    while True:
        job = claim_next_job()
        if job is None:
            time.sleep(1)
            continue
        job_id, data = job
        print(f"Processing email job {job_id}.", flush=True)
        process_job(job_id, data)


if __name__ == "__main__":
    main()