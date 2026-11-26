# run.py

import io
import os
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

from app.email_sender import (
    format_rim_entry,
    get_contacts_from_excel,
    normalize_mall_column,
    normalize_rim_type,
    pluralize,
    send_batch,
)
from app.job_store import create_job, get_job, seal_secret

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "app", "data")
app.secret_key = os.environ["SECRET_KEY"]

def _parse_add_tc_prefix(form):
    """Treat any explicit true value as enabled; preserve legacy true when absent."""
    values = [value.lower() for value in form.getlist("add_tc_prefix")]
    if not values:
        return True
    return any(value == "true" for value in values)


@app.route("/health")
def health_check():
    """Health check endpoint for monitoring and container orchestration."""
    return jsonify({"status": "healthy"}), 200


@app.route("/")
def index():
    if "MY_ADDRESS" not in session or "PASSWORD" not in session:
        return redirect(url_for("login"))

    template_dir = os.path.join(app.root_path, "app", "email_templates")

    templates = {}
    for name in ["check", "check_rim", "confirm", "new_rim", "close", "media"]:
        file_path = os.path.join(template_dir, f"{name}.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            templates[name] = f.read()
    return render_template(
        "index.html", templates=templates, default_template=templates["new_rim"]
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form["email"]
        password = request.form["password"]

        if not display_name and email:
            display_name = email.split("@")[0].replace(".", " ").title()

        if email and password:
            session["MY_ADDRESS"] = email
            session["PASSWORD"] = password
            session["DISPLAY_NAME"] = display_name
            return redirect(url_for("index"))
        return render_template("login.html", error="Заполните оба поля")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/preview-excel", methods=["POST"])
def preview_excel():
    file = request.files.get("contacts_file")
    if not file:
        return "❌ Файл не загружен.", 400

    ALLOWED_COLUMNS = [
        "email",
        "name",
        "city",
        "mall",
        "type",
        "rim",
        "link",
        "min",
        "sec",
        "num",
        "size",
        "email2",
        "name2",
    ]

    try:
        df = pd.read_excel(io.BytesIO(file.read()))
        df = df[[col for col in df.columns if col in ALLOWED_COLUMNS]]
        df = df.fillna("").astype(str).apply(lambda x: x.str.strip())

        # Check required columns BEFORE dropping any empty columns
        required_columns = {"email", "mall", "city"}
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            return (
                f"<div style='color:red;'>В файле отсутствуют обязательные столбцы: {', '.join(missing_columns)}</div>",
                400,
            )

        # Drop only non-required columns that are entirely empty
        cols_to_drop = [
            c for c in df.columns if c not in required_columns and df[c].eq("").all()
        ]
        if cols_to_drop:
            df = df.drop(columns=cols_to_drop)

        # Validate rows: email must not be empty
        if df["email"].eq("").any():
            return (
                "<div style='color:red;'>В файле есть строки без email. Удалите их или заполните.</div>",
                400,
            )

        add_prefix = _parse_add_tc_prefix(request.form)

        if "mall" in df.columns:
            df["mall"] = normalize_mall_column(df["mall"], add_prefix=add_prefix)

        if "type" in df.columns:
            df["type"] = df["type"].apply(normalize_rim_type)
        else:
            df["type"] = "digital"

        if "name" not in df.columns:
            df["name"] = ""
        df.loc[df["name"] == "", "name"] = "Коллеги"

        rims_required = {"rim", "num", "size", "link"}
        if rims_required.issubset(df.columns):
            df["rim"] = df.apply(format_rim_entry, axis=1)

        if "rim" in df.columns:
            df["rim"] = df["rim"].astype(str).str.strip()

            def join_rims(values):
                return "\n".join(v for v in values if v)

            df = df.groupby(["city", "mall", "email", "name"], as_index=False).agg(
                {"rim": join_rims}
            )
            df["rim"] = df["rim"].str.replace("\n", "<br>", regex=False)

        first_row = df.iloc[0].to_dict() if not df.empty else {}
        attrs = (
            f'data-mall="{first_row.get("mall", "")}" data-city="{first_row.get("city", "")}"'
            if first_row
            else ""
        )

        table_html = df.to_html(classes="preview-table", index=False, escape=False)
        return (
            f'<div id="first-row-data" {attrs} style="display:none;"></div>'
            + table_html
        )

    except Exception as e:
        return f"<div style='color:red;'>❌ Ошибка при чтении файла: {str(e)}</div>"


@app.route("/send-emails", methods=["POST"])
def send():
    display_name = session.get("DISPLAY_NAME")
    my_address = session.get("MY_ADDRESS")
    password = session.get("PASSWORD")

    if not my_address or not password:
        return render_template(
            "status.html", status="❌ Сессия истекла. Войдите снова."
        ), 401

    brand = request.form.get("brand", "").strip()
    period = request.form.get("period", "").strip()
    doc = request.form.get("doc", "").strip()

    cc_addresses = [
        email.strip()
        for email in request.form.get("cc_list", "").split(",")
        if email.strip()
    ]

    uploaded_file = request.files.get("contacts_file")
    if not uploaded_file or uploaded_file.filename == "":
        return render_template("status.html", status="❌ Файл не загружен.")

    job_id = str(uuid4())
    jobs_dir = os.path.join(app.config["UPLOAD_FOLDER"], "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    file_path = os.path.join(jobs_dir, f"{job_id}.xlsx")
    uploaded_file.save(file_path)
    template_text = request.form.get("message_template", "")
    add_prefix = _parse_add_tc_prefix(request.form)

    if not display_name and my_address:
        display_name = my_address.split("@")[0].replace(".", " ").title()

    try:
        batch_size = int(request.form.get("batch_size", "25"))
    except Exception:
        batch_size = 25
    try:
        pause_seconds = int(request.form.get("pause_seconds", "90"))
    except Exception:
        pause_seconds = 90

    job_data = {
        "file_path": file_path,
        "display_name": display_name,
        "my_address": my_address,
        "password": seal_secret(password),
        "brand": brand,
        "period": period,
        "doc": doc,
        "cc_addresses": cc_addresses,
        "template_text": template_text,
        "add_prefix": add_prefix,
        "batch_size": batch_size,
        "pause_seconds": pause_seconds,
    }
    try:
        create_job(job_id, job_data)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        app.logger.exception("Unable to enqueue email job")
        return render_template("status.html", status="❌ Не удалось создать задачу."), 500

    return render_template(
        "status.html", status="Задача создана, письма отправятся в очереди...", job_id=job_id
    )


@app.route("/send-status/<job_id>", methods=["GET"])
def send_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=False)
