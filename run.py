# run.py

import io
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from app.email_sender import send_emails, get_contacts_from_excel, pluralize
import os
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import traceback
import pandas as pd

load_dotenv()

app = Flask(
    __name__,
    template_folder='app/templates',
    static_folder='app/static'
)
app.config['UPLOAD_FOLDER'] = 'app/data'
app.secret_key = os.urandom(24)


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

    ALLOWED_COLUMNS = ["email", "name", "city", "mall", "rim", "link", "min", "sec", "num", "size"]

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
            prefixes = ("ТЦ", "ТРЦ", "ТРК", "ТД", "ТК", "Молл")
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
                return (f"{row['rim']} {row['num']} шт. {row['size']} (ролик {row['sec']}сек в блоке {row['min']} мин.) фото: {row['link']}")
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

    try:
        contacts = get_contacts_from_excel(file_path, template_text=template_text, doc=doc, add_prefix=add_prefix)
        send_emails(
            my_address=my_address,
            password=password,
            contacts=contacts,
            cc_addresses=cc_addresses,
            brand=brand,
            period=period,
            doc=doc,
            template_text=template_text,
            display_name=display_name
        )
        count = len(contacts)
        word = pluralize(count, ("адрес", "адреса", "адресов"))
        status = f"✅ Письма успешно отправлены на {count} {word}."
    except Exception as e:
        status = f"❌ Ошибка: {str(e)}"
    return render_template("status.html", status=status)


if __name__ == '__main__':
    app.run(debug=True)