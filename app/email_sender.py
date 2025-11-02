# app/email_sender.py

import smtplib
import ssl
import re
import pandas as pd
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from string import Template
from pathlib import Path
import time
from math import ceil


# SMTP settings
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_PROTOCOL = os.getenv("SMTP_PROTOCOL", "SSL").upper()


def get_contacts_from_excel(filepath, template_text=None, doc=None, add_prefix=True):
    df = pd.read_excel(filepath)
    if 'email' not in df.columns:
        raise ValueError("❌ Нет обязательного столбца: email")
    
    df = df.fillna('').astype(str).apply(lambda x: x.str.strip())
        
    email_regex = r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$'

    for col in ['email', 'name', 'mall', 'city', 'rim']:
        if col in df.columns:
            df[col] = df[col].fillna('').astype(str).str.strip()
    
    if 'name' in df.columns:
        df.loc[df['name'] == '', 'name'] = 'Коллеги'

    if 'mall' in df.columns:
        prefixes = ("ТЦ", "ТРЦ", "ТРК", "ТД", "ТК", "Молл")
        # normalize column: replace quotes, turn NaN -> empty string, strip spaces
        df['mall'] = df['mall'].fillna('').astype(str).str.replace(r'[«»"]', '', regex=True).str.strip()

        if add_prefix:
            # build regex to detect any prefix at start, case-insensitive
            pat = r'^(?:' + '|'.join(prefixes) + r')\b'
            # mask of rows that don't already start with a prefix and are non-empty
            mask = (~df['mall'].str.match(pat, case=False, na=False)) & (df['mall'] != '')
            df.loc[mask, 'mall'] = 'ТЦ ' + df.loc[mask, 'mall']

    rims_required = {'rim', 'num', 'size', 'link', 'min', 'sec'}
    if rims_required.issubset(df.columns):
        def format_rim_entry(row):
            return (f"{row['rim']} {row['num']} шт. {row['size']} (ролик {row['sec']}сек в блоке {row['min']} мин.) фото: {row['link']}").strip()

        df['rim'] = df.apply(format_rim_entry, axis=1)
    
    contacts = []
    for idx, row in df.iterrows():
        parts = split_emails(row['email'])
        if not parts:
            raise ValueError(f"❌ Строка {idx + 2} не содержит email. Удалите её или заполните.")
        for e in parts:
            if not re.match(email_regex, e):
                raise ValueError(f"❌ Неверный формат email: {e} в строке {idx + 2}")
        primary = parts[0]
        cc = parts[1:]
        contact = {
            'email': primary,
            'name': row.get('name', ''),
            'mall': row.get('mall', ''),
            'city': row.get('city', ''),
            'rim': row.get('rim', ''),
            '_cc_emails': cc
        }
        contacts.append(contact)
    
    df = pd.DataFrame(contacts)

    if 'rim' in df.columns and 'mall' in df.columns:
        combined_contacts = []
        for email, email_df in df.groupby('email', as_index=False):
            name = email_df['name'].iloc[0]
            city = email_df['city'].iloc[0]
            cc_emails = email_df['_cc_emails'].iloc[0] if '_cc_emails' in email_df.columns else []

            mall_rims_list = []
            mall_names_list = []

            for mall_name, mall_df in email_df.groupby('mall', as_index=False):
                # Add prefix if not already there
                if add_prefix:
                    prefixes = ("ТЦ", "ТРЦ", "ТРК", "ТД", "ТК", "Молл")
                    pat = r'^(?:' + '|'.join(prefixes) + r')\b'
                    if not re.match(pat, mall_name, re.IGNORECASE):
                        mall_name_prefixed = "ТЦ " + mall_name
                    else:
                        mall_name_prefixed = mall_name
                else:
                    mall_name_prefixed = mall_name

                mall_names_list.append(mall_name_prefixed)
                rims_text = '\n'.join(mall_df['rim'].astype(str))
                if email_df['mall'].nunique() > 1:
                    mall_rims_list.append(f"{mall_name_prefixed}:\n{rims_text}")
                else:
                    mall_rims_list.append(rims_text)

            rim_combined = "\n\n".join(mall_rims_list)
            mall_combined = " и ".join(mall_names_list)

            combined_contacts.append({
                'email': email,
                'name': name,
                'city': city,
                'rim': rim_combined,
                'mall': mall_combined,
                '_cc_emails': cc_emails
            })

        df = pd.DataFrame(combined_contacts)

 
    if template_text:
        required_map = {
            "RIM": "rim",
            "LINK": "link",
            "MIN": "min",
            "SEC": "sec"
        }
        placeholders = set(re.findall(r"\$\{(\w+)\}", template_text))

        needed_columns = [required_map[p] for p in placeholders if p in required_map]
        missing_cols = [col for col in needed_columns if col not in df.columns]
        if missing_cols:
            raise ValueError(f"❌ Нет необходимого столбца(ов): {', '.join(missing_cols)}")

        empty_required = []
        for ph, col in required_map.items():
            if ph in placeholders and col in df.columns:
                if df[col].fillna('').astype(str).str.strip().eq('').any():
                    empty_required.append(col)
        if empty_required:
            raise ValueError(
                "❌ В обязательных столбцах есть пустые значения: "
                + ", ".join(empty_required)
                + ". Заполните их или удалите строки."
            )

        if "DOC" in placeholders and not (doc and str(doc).strip()):
            raise ValueError("❌ Нет необходимого поля doc (ссылка)")

    return df.to_dict(orient='records')


def read_template(template_path):
    with open(template_path, 'r', encoding='utf-8') as file:
        return Template(file.read())


def send_emails(my_address, password, contacts, cc_addresses, brand, period, doc, template_text, display_name, batch_size=1, pause_seconds=120, progress_callback=None):

    template = Template(template_text)
    context = ssl.create_default_context()
    cc_addresses = cc_addresses or []

    total_contacts = len(contacts)
    total_batches = ceil(total_contacts / batch_size)

    for batch_index in range(total_batches):
        start = batch_index * batch_size
        end = min(start + batch_size, total_contacts)
        batch_contacts = contacts[start:end]

        if SMTP_PROTOCOL == "SSL":
            server_cm = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context)
        else:
            server_cm = smtplib.SMTP(SMTP_HOST, SMTP_PORT)

        with server_cm as server:
            server.set_debuglevel(1)
            server.ehlo()
            if SMTP_PROTOCOL == "STARTTLS":
                server.starttls(context=context)
                server.ehlo()

            server.login(my_address, password)

            for contact in batch_contacts:
                msg = MIMEMultipart()
                mall_name = contact['mall'].replace('"', '')

                message = template.safe_substitute(
                    NAME=contact['name'],
                    BRAND=brand,
                    PERIOD=period,
                    MALL=mall_name,
                    RIM=contact.get('rim', ''),
                    LINK=contact.get('link', ''),
                    MIN=contact.get('min', ''),
                    SEC=contact.get('sec', ''),
                    DOC=doc or ""
                )

                contact_cc = contact.get('_cc_emails', [])
                all_cc = list(set(cc_addresses + contact_cc))

                msg['From'] = formataddr((display_name, my_address))
                msg['To'] = contact['email']
                if all_cc:
                    msg['Cc'] = ", ".join(all_cc)
                msg['Subject'] = f"{mall_name} (г. {contact['city']}) // {brand} // {period}"
                msg.attach(MIMEText(message, 'plain'))

                recipients = [contact['email']] + all_cc
                server.send_message(msg, from_addr=my_address, to_addrs=recipients)
                del msg

        sent_count = end
        if progress_callback:
            progress_callback(batch_index + 1, total_batches, sent_count)

        if batch_index + 1 < total_batches:
            print(f"Waiting {pause_seconds} seconds before next batch ({batch_index + 1}/{total_batches})...")
            time.sleep(pause_seconds)


def split_emails(email_str):
    s = str(email_str)
    for sep in [',', ';', '/', '|', ' и ']:
        s = s.replace(sep, ' ')
    parts = [p.strip() for p in s.split() if p.strip()]
    return parts


def pluralize(n, forms):

    n = abs(n) % 100
    n1 = n % 10

    if 10 < n < 20:
        return forms[2]
    if 1 < n1 < 5:
        return forms[1]
    if n1 == 1:
        return forms[0]
    return forms[2]