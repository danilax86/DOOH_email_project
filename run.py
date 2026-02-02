# run.py

import io
import math
import os
import threading
import time
import traceback
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from app.email_sender import get_contacts_from_excel, pluralize, send_batch

load_dotenv()

app = Flask(
    __name__,
    template_folder='app/templates',
    static_folder='app/static'
)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'app', 'data')
app.secret_key = os.urandom(24)

# simple in-memory job registry for background sending tasks
# job structure: { 'status': str, 'batch': int, 'total': int or None, 'sent': int, 'error': str or None, 'done': bool, 'done_at': float or None }
app.jobs = {}
JOB_RETENTION_SECONDS = 7200
JOB_REGISTRY_MAX_SIZE = 500


def _evict_old_jobs():
    """Remove done jobs older than JOB_RETENTION_SECONDS and cap registry at JOB_REGISTRY_MAX_SIZE to avoid unbounded memory growth."""
    now = time.time()
    cutoff = now - JOB_RETENTION_SECONDS
    to_remove = [jid for jid, job in app.jobs.items() if job.get('done') and job.get('done_at', 0) < cutoff]
    for jid in to_remove:
        del app.jobs[jid]
    while len(app.jobs) > JOB_REGISTRY_MAX_SIZE:
        done_ids = [jid for jid, job in app.jobs.items() if job.get('done')]
        if not done_ids:
            break
        oldest = min(done_ids, key=lambda jid: app.jobs[jid].get('done_at', 0))
        del app.jobs[oldest]


@app.route('/health')
def health_check():
    """Health check endpoint for monitoring and container orchestration."""
    return jsonify({"status": "healthy"}), 200


@app.route('/')
def index():
    if 'MY_ADDRESS' not in session or 'PASSWORD' not in session:
        return redirect(url_for('login'))

    template_dir = os.path.join(app.root_path, 'app', 'email_templates')

    templates = {}
    for name in ['check', 'check_rim', 'confirm', 'new_rim', 'close', 'media']:
        file_path = os.path.join(template_dir, f"{name}.txt")
        with open(file_path, 'r', encoding='utf-8') as f:
            templates[name] = f.read()
    return render_template('index.html', templates=templates, default_template=templates['new_rim'])


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        display_name = request.form.get('display_name', '').strip()
        email = request.form['email']
        password = request.form['password']

        if not display_name and email:
            display_name = email.split('@')[0].replace('.', ' ').title()

        if email and password:
            session['MY_ADDRESS'] = email
            session['PASSWORD'] = password
            session['DISPLAY_NAME'] = display_name
            return redirect(url_for('index'))
        return render_template('login.html', error="Заполните оба поля")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/preview-excel', methods=['POST'])
def preview_excel():
    file = request.files.get('contacts_file')
    if not file:
        return "❌ Файл не загружен.", 400

    ALLOWED_COLUMNS = ["email", "name", "city", "mall", "rim", "link", "min", "sec", "num", "size", "email2", "name2"]

    try:
        df = pd.read_excel(io.BytesIO(file.read()))
        df = df[[col for col in df.columns if col in ALLOWED_COLUMNS]]
        df = df.fillna('').astype(str).apply(lambda x: x.str.strip())

        # Check required columns BEFORE dropping any empty columns
        required_columns = {"email", "mall", "city"}
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            return f"<div style='color:red;'>❌ В файле отсутствуют обязательные столбцы: {', '.join(missing_columns)}</div>", 400

        # Drop only non-required columns that are entirely empty
        cols_to_drop = [c for c in df.columns if c not in required_columns and df[c].eq('').all()]
        if cols_to_drop:
            df = df.drop(columns=cols_to_drop)

        # Validate rows: email must not be empty
        if df['email'].eq('').any():
            return "<div style='color:red;'>❌ В файле есть строки без email. Удалите их или заполните.</div>", 400

        add_prefix = request.form.get('add_tc_prefix', 'true').lower() == 'true'

        if 'mall' in df.columns:
            prefixes = ("ТЦ", "ТРЦ", "ТРК", "ТД", "ТК", "Молл", "ТВК", "МТЦ", "МЦ")
            # normalize column: replace quotes, turn NaN -> empty string, strip spaces
            df['mall'] = df['mall'].fillna('').astype(str).str.replace('"', '', regex=False).str.strip()

            if add_prefix:
                # build regex to detect any prefix at start, case-insensitive
                pat = r'^(?:' + '|'.join(prefixes) + r')\b'
                # mask of rows that don't already start with a prefix and are non-empty
                mask = (~df['mall'].str.match(pat, case=False, na=False)) & (df['mall'] != '')
                df.loc[mask, 'mall'] = 'ТЦ ' + df.loc[mask, 'mall']

        if 'name' not in df.columns:
            df['name'] = ''
        df.loc[df['name'] == '', 'name'] = 'Коллеги'

        rims_required = {'rim', 'num', 'size', 'link', 'min', 'sec'}
        if rims_required.issubset(df.columns):
            print('rims are in place')
            def format_rim_entry(row):
                # Safely get values and strip whitespace
                rim = str(row.get('rim', '')).strip()
                num = str(row.get('num', '')).strip()
                size = str(row.get('size', '')).strip()
                link = str(row.get('link', '')).strip()
                min_ = str(row.get('min', '')).strip()
                sec = str(row.get('sec', '')).strip()

                # Case: all parts present
                if num and size and sec and min_ and link:
                    return f"{rim} {num} шт. {size} (ролик {sec}сек в блоке {min_} мин.) фото: {link}"

                # No num and size, but have sec/min/link
                if (not num and not size) and sec and min_ and link:
                    return f"{rim} (ролик {sec}сек в блоке {min_} мин.) фото: {link}"

                # Have sec/min but no link -> omit photo
                if sec and min_ and not link:
                    # include num/size if present
                    if num and size:
                        return f"{rim} {num} шт. {size} (ролик {sec}сек в блоке {min_} мин.)"
                    return f"{rim} (ролик {sec}сек в блоке {min_} мин.)"

                # No num/size/link but have sec/min -> same as above
                if (not num and not size and not link) and sec and min_:
                    return f"{rim} (ролик {sec}сек в блоке {min_} мин.)"

                # No timing but have link -> show rim and photo
                if (not num and not size and not sec and not min_) and link:
                    return f"{rim}, фото: {link}"

                # Fallback: compose available parts
                parts = [p for p in [rim] if p]
                if num and size:
                    parts.append(f"{num} шт. {size}")
                if sec and min_:
                    parts.append(f"(ролик {sec}сек в блоке {min_} мин.)")
                elif sec:
                    parts.append(f"(ролик {sec}сек)")
                if link:
                    parts.append(f"фото: {link}")

                return ' '.join(parts).strip()

            df['rim'] = df.apply(format_rim_entry, axis=1)

        if 'rim' in df.columns:
            df['rim'] = df['rim'].astype(str).str.strip()
            def join_rims(values):
                return '\n'.join(v for v in values if v)
            df = (df.groupby(['city', 'mall', 'email', 'name'], as_index=False)
                  .agg({'rim': join_rims}))
            df['rim'] = df['rim'].str.replace('\n', '<br>', regex=False)

        first_row = df.iloc[0].to_dict() if not df.empty else {}
        attrs = f'data-mall="{first_row.get("mall", "")}" data-city="{first_row.get("city", "")}"' if first_row else ""

        table_html = df.to_html(classes="preview-table", index=False, escape=False)
        return f'<div id="first-row-data" {attrs} style="display:none;"></div>' + table_html

    except Exception as e:
        return f"<div style='color:red;'>❌ Ошибка при чтении файла: {str(e)}</div>"


@app.route('/send-emails', methods=['POST'])
def send():
    display_name = session.get("DISPLAY_NAME")
    my_address = session.get("MY_ADDRESS")
    password = session.get("PASSWORD")

    if not my_address or not password:
        return render_template("status.html", status="❌ Сессия истекла. Войдите снова."), 401

    brand = request.form.get('brand', '').strip()
    period = request.form.get('period', '').strip()
    doc = request.form.get('doc', '').strip()

    cc_addresses = [email.strip() for email in request.form.get('cc_list', '').split(',') if email.strip()]

    uploaded_file = request.files.get('contacts_file')
    if not uploaded_file or uploaded_file.filename == '':
        return render_template("status.html", status="❌ Файл не загружен.")

    filename = secure_filename(uploaded_file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    uploaded_file.save(file_path)
    template_text = request.form.get('message_template', '')
    add_prefix = request.form.get('add_tc_prefix', 'true').lower() == 'true'

    if not display_name and my_address:
        display_name = my_address.split('@')[0].replace('.', ' ').title()

    _evict_old_jobs()
    job_id = str(uuid4())
    app.jobs[job_id] = {'status': 'Queued', 'batch': 0, 'total': None, 'sent': 0, 'error': None, 'done': False}

    def run_job():
        try:
            contacts = get_contacts_from_excel(file_path, template_text=template_text, doc=doc, add_prefix=add_prefix)
            total_contacts = len(contacts)
            try:
                batch_size = int(request.form.get('batch_size', '25'))
            except Exception:
                batch_size = 25
            try:
                pause_seconds = int(request.form.get('pause_seconds', '90'))
            except Exception:
                pause_seconds = 90
            total_batches = math.ceil(total_contacts / batch_size) if total_contacts > 0 else 0
            app.jobs[job_id].update({'status': 'Running', 'total': total_batches})
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
                        display_name=display_name
                    )
                except Exception as e:
                    app.jobs[job_id].update({'status': f"❌ Ошибка при отправке: {str(e)}", 'error': str(e), 'done': True, 'done_at': time.time()})
                    return
                cumulative_sent += sent
                app.jobs[job_id].update({
                    'status': f'Отправлено {batch_index + 1}/{total_batches}',
                    'batch': batch_index + 1,
                    'total': total_batches,
                    'sent': cumulative_sent
                })
                if batch_index + 1 < total_batches:
                    print(f"Waiting {pause_seconds} seconds before next batch ({batch_index + 1}/{total_batches})...")
                    time.sleep(pause_seconds)
            count = len(contacts)
            word = pluralize(count, ("адрес", "адреса", "адресов"))
            app.jobs[job_id].update({'status': f"✅ Письма успешно отправлены на {count} {word}.", 'done': True, 'done_at': time.time()})
        except Exception as e:
            app.jobs[job_id].update({'status': f"❌ Ошибка: {str(e)}", 'error': str(e), 'done': True, 'done_at': time.time()})
        finally:
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except OSError:
                pass

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    # return a page that will poll for progress
    return render_template("status.html", status="Письма отправляются...", job_id=job_id)


@app.route('/send-status/<job_id>', methods=['GET'])
def send_status(job_id):
    job = app.jobs.get(job_id)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify(job)


if __name__ == '__main__':
    app.run(debug=False)
