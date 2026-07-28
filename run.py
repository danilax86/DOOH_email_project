# run.py

import io
import json
import math
import os
import threading
import time
from uuid import uuid4

import pandas as pd

try:
    from app.sheet_transformer import (
        filters_from_json,
        inspect_sheet_source,
        transform_sheet_source,
        workbook_to_bytes,
    )

    SHEET_TRANSFORMER_AVAILABLE = True
except ModuleNotFoundError:
    SHEET_TRANSFORMER_AVAILABLE = False
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from app.email_sender import (
    format_rim_entry,
    get_contacts_from_excel,
    pluralize,
    send_batch,
)

load_dotenv()

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "app", "data")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

# simple in-memory registries for background state
# job structure: { 'status': str, 'batch': int, 'total': int or None, 'sent': int, 'error': str or None, 'done': bool, 'done_at': float or None }
app.jobs = {}
app.sheet_states = {}
jobs_lock = threading.RLock()
sheet_states_lock = threading.RLock()
JOB_RETENTION_SECONDS = 7200
JOB_REGISTRY_MAX_SIZE = 500
SHEET_STATE_RETENTION_SECONDS = 6 * 60 * 60
SHEET_STATE_MAX_SIZE = 100


def _evict_old_jobs():
    """Remove done jobs older than JOB_RETENTION_SECONDS and cap registry at JOB_REGISTRY_MAX_SIZE to avoid unbounded memory growth."""
    now = time.time()
    cutoff = now - JOB_RETENTION_SECONDS
    with jobs_lock:
        to_remove = [
            jid
            for jid, job in app.jobs.items()
            if job.get("done") and job.get("done_at", 0) < cutoff
        ]
        for jid in to_remove:
            del app.jobs[jid]
        while len(app.jobs) > JOB_REGISTRY_MAX_SIZE:
            done_ids = [jid for jid, job in app.jobs.items() if job.get("done")]
            if not done_ids:
                break
            oldest = min(done_ids, key=lambda jid: app.jobs[jid].get("done_at", 0))
            del app.jobs[oldest]


def _update_job(job_id, **updates):
    with jobs_lock:
        job = app.jobs.get(job_id)
        if job is not None:
            job.update(updates)


def _get_job(job_id):
    with jobs_lock:
        job = app.jobs.get(job_id)
        if job is None:
            return None
        return dict(job)


def _evict_old_sheet_states():
    now = time.time()
    cutoff = now - SHEET_STATE_RETENTION_SECONDS
    with sheet_states_lock:
        to_remove = [
            state_id
            for state_id, state in app.sheet_states.items()
            if state.get("updated_at", 0) < cutoff
        ]
        for state_id in to_remove:
            del app.sheet_states[state_id]
        while len(app.sheet_states) > SHEET_STATE_MAX_SIZE:
            oldest_state_id = min(
                app.sheet_states,
                key=lambda sid: app.sheet_states[sid].get("updated_at", 0),
            )
            del app.sheet_states[oldest_state_id]


def _get_sheet_state(create=False):
    state_id = session.get("SHEET_STATE_ID")
    if not state_id and create:
        state_id = str(uuid4())
        session["SHEET_STATE_ID"] = state_id
    if not state_id:
        return None, None
    with sheet_states_lock:
        state = app.sheet_states.get(state_id)
        if state is None and create:
            state = {"updated_at": time.time()}
            app.sheet_states[state_id] = state
        return state_id, state


def _store_sheet_state(payload):
    _evict_old_sheet_states()
    state_id, state = _get_sheet_state(create=True)
    if state is None:
        state = {}
    with sheet_states_lock:
        state.update(payload)
        state["updated_at"] = time.time()
        app.sheet_states[state_id] = state
    return state_id, state


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


@app.route("/sheet", methods=["GET"])
def sheet_page():
    if not SHEET_TRANSFORMER_AVAILABLE:
        return (
            render_template(
                "status.html", status="❌ Sheet module is not installed on server."
            ),
            503,
        )
    return render_template("sheet.html")


@app.route("/sheet/state", methods=["GET", "POST"])
def sheet_state():
    if not SHEET_TRANSFORMER_AVAILABLE:
        return jsonify(
            {"error": "Sheet tools are not available on this deployment"}
        ), 503

    if request.method == "POST":
        state_id, state = _get_sheet_state(create=True)
        if not state:
            return jsonify({"error": "state unavailable"}), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid payload"}), 400

        with sheet_states_lock:
            if "selected_columns" in payload:
                state["selected_columns"] = (
                    payload.get("selected_columns")
                    if isinstance(payload.get("selected_columns"), list)
                    else []
                )
            if "filters" in payload:
                state["filters"] = (
                    payload.get("filters")
                    if isinstance(payload.get("filters"), dict)
                    else {}
                )

            state["updated_at"] = time.time()
            app.sheet_states[state_id] = state
        return jsonify({"ok": True})

    state_id, state = _get_sheet_state(create=False)
    if not state:
        return jsonify({"has_state": False})

    with sheet_states_lock:
        state_snapshot = dict(state)
    inspection = state_snapshot.get("inspection") or {}
    return jsonify(
        {
            "has_state": True,
            "sheet_name": inspection.get("sheet_name"),
            "header_row": inspection.get("header_row"),
            "columns": inspection.get("columns", []),
            "row_count": inspection.get("row_count", 0),
            "filter_options": inspection.get("filter_options", {}),
            "preview_rows": inspection.get("preview_rows", []),
            "selected_columns": state_snapshot.get(
                "selected_columns", inspection.get("columns", [])
            ),
            "filters": state_snapshot.get("filters", {}),
            "source_type": state_snapshot.get("source_type"),
            "source_name": state_snapshot.get("source_name"),
        }
    )


@app.route("/sheet/inspect", methods=["POST"])
def sheet_inspect():
    if not SHEET_TRANSFORMER_AVAILABLE:
        return jsonify(
            {"error": "Sheet tools are not available on this deployment"}
        ), 503

    uploaded_file = request.files.get("source_file")
    source_url = request.form.get("source_url", "").strip() or None

    if not uploaded_file and not source_url:
        return jsonify({"error": "Нужно загрузить файл или указать ссылку"}), 400

    try:
        source_bytes = None
        source_name = None
        if uploaded_file and uploaded_file.filename:
            source_bytes = uploaded_file.read()
            source_name = uploaded_file.filename
            source_file = io.BytesIO(source_bytes)
        else:
            source_file = None

        inspection = inspect_sheet_source(
            source_file=source_file, source_url=source_url
        )
        selected_columns = inspection.get("columns", [])
        _store_sheet_state(
            {
                "source_type": "file" if source_bytes is not None else "url",
                "source_url": source_url,
                "source_name": source_name,
                "source_bytes": source_bytes,
                "inspection": inspection,
                "selected_columns": selected_columns,
                "filters": {},
            }
        )
        return jsonify(inspection)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/sheet/export", methods=["POST"])
def sheet_export():
    if not SHEET_TRANSFORMER_AVAILABLE:
        return jsonify(
            {"error": "Sheet tools are not available on this deployment"}
        ), 503

    uploaded_file = request.files.get("source_file")
    source_url = request.form.get("source_url", "").strip() or None
    brand = request.form.get("brand", "").strip()
    info_text = request.form.get("info_text", "").strip()
    try:
        selected_columns = json.loads(
            request.form.get("selected_columns_json", "[]") or "[]"
        )
    except json.JSONDecodeError:
        selected_columns = []
    if not isinstance(selected_columns, list):
        selected_columns = []
    filters = filters_from_json(request.form.get("filters_json"))

    if not uploaded_file and not source_url:
        _, state = _get_sheet_state(create=False)
        if state:
            if state.get("source_type") == "file" and state.get("source_bytes"):
                uploaded_file = io.BytesIO(state["source_bytes"])
                source_url = None
            elif state.get("source_url"):
                source_url = state.get("source_url")
            if not brand:
                brand = request.form.get("brand", "").strip()

    if not uploaded_file and not source_url:
        return render_template(
            "status.html", status="❌ Нужен XLSX-файл или ссылка на Google Sheets."
        ), 400

    if not brand:
        return render_template("status.html", status="❌ Укажите бренд."), 400

    try:
        workbook, info = transform_sheet_source(
            source_file=uploaded_file,
            source_url=source_url,
            brand=brand,
            info_text=info_text,
            selected_columns=selected_columns,
            filters=filters,
        )
        _store_sheet_state(
            {
                "source_type": "file"
                if hasattr(uploaded_file, "read") and not isinstance(uploaded_file, str)
                else "url",
                "source_url": source_url,
                "inspection": _get_sheet_state(create=False)[1].get("inspection")
                if _get_sheet_state(create=False)[1]
                else {},
                "selected_columns": selected_columns,
                "filters": filters,
            }
        )
        filename = secure_filename(f"{brand or 'report'}_sheet_report.xlsx")
        response = send_file(
            io.BytesIO(workbook_to_bytes(workbook)),
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response.headers["X-Source-Rows"] = str(info["row_count"])
        response.headers["X-Filtered-Rows"] = str(info["filtered_row_count"])
        return response
    except Exception as exc:
        return render_template("status.html", status=f"❌ Ошибка: {exc}"), 400


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
                f"<div style='color:red;'>❌ В файле отсутствуют обязательные столбцы: {', '.join(missing_columns)}</div>",
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
                "<div style='color:red;'>❌ В файле есть строки без email. Удалите их или заполните.</div>",
                400,
            )

        add_prefix = request.form.get("add_tc_prefix", "true").lower() == "true"

        if "mall" in df.columns:
            prefixes = ("ТЦ", "ТРЦ", "ТРК", "ТД", "ТК", "Молл", "ТВК", "МТЦ", "МЦ")
            # normalize column: replace quotes, turn NaN -> empty string, strip spaces
            df["mall"] = (
                df["mall"]
                .fillna("")
                .astype(str)
                .str.replace('"', "", regex=False)
                .str.strip()
            )

            if add_prefix:
                # build regex to detect any prefix at start, case-insensitive
                pat = r"^(?:" + "|".join(prefixes) + r")\b"
                # mask of rows that don't already start with a prefix and are non-empty
                mask = (~df["mall"].str.match(pat, case=False, na=False)) & (
                    df["mall"] != ""
                )
                df.loc[mask, "mall"] = "ТЦ " + df.loc[mask, "mall"]

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

    template_text = request.form.get("message_template", "")
    add_prefix = request.form.get("add_tc_prefix", "true").lower() == "true"

    if not display_name and my_address:
        display_name = my_address.split("@")[0].replace(".", " ").title()

    try:
        batch_size = int(request.form.get("batch_size", "25"))
    except Exception:
        batch_size = 25
    batch_size = max(1, batch_size)

    try:
        pause_seconds = int(request.form.get("pause_seconds", "90"))
    except Exception:
        pause_seconds = 90
    pause_seconds = max(0, pause_seconds)

    _evict_old_jobs()
    job_id = str(uuid4())
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"contacts_{job_id}.xlsx")
    uploaded_file.save(file_path)

    with jobs_lock:
        app.jobs[job_id] = {
            "status": "Queued",
            "batch": 0,
            "total": None,
            "sent": 0,
            "error": None,
            "done": False,
            "done_at": None,
        }

    def run_job():
        try:
            contacts = get_contacts_from_excel(
                file_path, template_text=template_text, doc=doc, add_prefix=add_prefix
            )
            total_contacts = len(contacts)
            total_batches = (
                math.ceil(total_contacts / batch_size) if total_contacts > 0 else 0
            )
            _update_job(job_id, status="Running", total=total_batches)
            cumulative_sent = 0
            for batch_index in range(total_batches):
                start = batch_index * batch_size
                end = min(start + batch_size, total_contacts)
                batch_contacts = contacts[start:end]
                try:
                    sent = send_batch(
                        my_address=my_address,
                        password=password,
                        batch_contacts=batch_contacts,
                        cc_addresses=cc_addresses,
                        brand=brand,
                        period=period,
                        doc=doc,
                        template_text=template_text,
                        display_name=display_name,
                    )
                except Exception as e:
                    _update_job(
                        job_id,
                        status=f"❌ Ошибка при отправке: {str(e)}",
                        error=str(e),
                        done=True,
                        done_at=time.time(),
                    )
                    return

                cumulative_sent += sent
                _update_job(
                    job_id,
                    status=f"Отправлено {batch_index + 1}/{total_batches}",
                    batch=batch_index + 1,
                    total=total_batches,
                    sent=cumulative_sent,
                )
                if batch_index + 1 < total_batches:
                    print(
                        f"Waiting {pause_seconds} seconds before next batch ({batch_index + 1}/{total_batches})..."
                    )
                    time.sleep(pause_seconds)

            count = len(contacts)
            word = pluralize(count, ("адрес", "адреса", "адресов"))
            _update_job(
                job_id,
                status=f"✅ Письма успешно отправлены на {count} {word}.",
                done=True,
                done_at=time.time(),
            )
        except Exception as e:
            _update_job(
                job_id,
                status=f"❌ Ошибка: {str(e)}",
                error=str(e),
                done=True,
                done_at=time.time(),
            )
        finally:
            try:
                uploaded_file.close()
            except Exception:
                pass
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    # return a page that will poll for progress
    return render_template(
        "status.html", status="Письма отправляются...", job_id=job_id
    )


@app.route("/send-status/<job_id>", methods=["GET"])
def send_status(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=False)
