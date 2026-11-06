import csv
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
from datetime import date

import pandas as pd
from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename
try:
    import pdfplumber
except ImportError:  # CSV imports should remain available without the optional PDF parser.
    pdfplumber = None

from analyzer import analyze, normalize_rows, suggest_mapping

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BANKING_DB", os.path.join(BASE, "instance", "banking.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["PHONE_MODE"] = False
app.config["PHONE_ACCESS_CODE"] = ""
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
          id INTEGER PRIMARY KEY, date TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL CHECK(amount > 0),
          type TEXT NOT NULL CHECK(type IN ('credit','debit')), currency TEXT NOT NULL, bank_name TEXT NOT NULL,
          account_label TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Uncategorized',
          fingerprint TEXT NOT NULL UNIQUE, review_status TEXT NOT NULL DEFAULT 'unreviewed',
          dataset TEXT NOT NULL DEFAULT 'csv', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_tx_bank_currency ON transactions(bank_name,currency);
        CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_label);
        CREATE TABLE IF NOT EXISTS imports (
          id INTEGER PRIMARY KEY, filename TEXT NOT NULL, imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
          accepted INTEGER NOT NULL, rejected INTEGER NOT NULL, duplicates INTEGER NOT NULL, report_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS credit_scores (
          id INTEGER PRIMARY KEY, score INTEGER NOT NULL CHECK(score BETWEEN 300 AND 900),
          recorded_on TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'CIBIL', notes TEXT NOT NULL DEFAULT '',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_credit_scores_date ON credit_scores(recorded_on DESC,id DESC);
        CREATE TABLE IF NOT EXISTS loans (
          id INTEGER PRIMARY KEY, lender TEXT NOT NULL, loan_type TEXT NOT NULL,
          original_amount REAL NOT NULL CHECK(original_amount > 0), currency TEXT NOT NULL DEFAULT 'INR',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS loan_updates (
          id INTEGER PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
          outstanding_balance REAL NOT NULL CHECK(outstanding_balance >= 0),
          emi_amount REAL NOT NULL CHECK(emi_amount >= 0), interest_rate REAL,
          next_due_date TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '', updated_on TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_loan_updates_latest ON loan_updates(loan_id,id DESC);
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(transactions)")}
        if "dataset" not in columns:
            con.execute("ALTER TABLE transactions ADD COLUMN dataset TEXT NOT NULL DEFAULT 'csv'")
            latest_import = con.execute("SELECT filename FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            if latest_import and str(latest_import[0]).lower().endswith(".pdf"):
                con.execute("UPDATE transactions SET dataset='pdf'")
                con.execute("UPDATE transactions SET fingerprint='pdf:' || fingerprint")
        if not con.execute("SELECT 1 FROM transactions WHERE dataset='csv' LIMIT 1").fetchone():
            sample_path = os.path.join(BASE, "sample_transactions.csv")
            if os.path.exists(sample_path):
                sample_df = pd.read_csv(sample_path, dtype=str, keep_default_na=False)
                sample_rows, _ = normalize_rows(sample_df, suggest_mapping(sample_df.columns), "Sample Bank", "", "INR", True)
                for row in sample_rows:
                    row["dataset"] = "csv"
                    con.execute("INSERT OR IGNORE INTO transactions(date,description,amount,type,currency,bank_name,account_label,category,fingerprint,dataset) VALUES(:date,:description,:amount,:type,:currency,:bank_name,:account_label,:category,:fingerprint,:dataset)", row)


init_db()


@app.before_request
def require_phone_access():
    """Protect every LAN page with a local passphrase and guard state-changing forms."""
    if not app.config["PHONE_MODE"]:
        return
    if request.endpoint in ("phone_login", "static"):
        return
    if not session.get("phone_authorized"):
        return redirect(url_for("phone_login"))
    if request.method == "POST":
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="Invalid form token. Reload and try again.")


@app.context_processor
def inject_csrf_token():
    def csrf_token():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token
    return {"csrf_token": csrf_token}


@app.route("/phone-login", methods=["GET", "POST"])
def phone_login():
    if not app.config["PHONE_MODE"]:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        supplied = request.form.get("access_code", "")
        if hmac.compare_digest(supplied, app.config["PHONE_ACCESS_CODE"]):
            session.clear()
            session["phone_authorized"] = True
            flash("This phone is connected to your local demo.", "success")
            return redirect(url_for("dashboard"))
        flash("That access phrase was not accepted.", "danger")
    return render_template("phone_login.html")


def filtered(filters=None):
    filters = filters or request.args
    dataset = filters.get("dataset", "csv")
    if dataset not in {"csv", "pdf"}:
        dataset = "csv"
    clauses, values = ["dataset=?"], [dataset]
    for field, arg in (("bank_name", "bank"), ("currency", "currency"), ("type", "type"), ("category", "category")):
        val = filters.get(arg, "")
        if val: clauses.append(f"{field}=?"); values.append(val)
    for arg, op in (("from", ">="), ("to", "<=")):
        val = filters.get(arg, "")
        if val: clauses.append(f"date {op} ?"); values.append(val)
    for field, arg in (("account_label", "account"),):
        val = filters.get(arg, "")
        if val: clauses.append(f"{field} LIKE ?"); values.append(f"%{val[:100]}%")
    for arg, op in (("min_amount", ">="), ("max_amount", "<=")):
        val = filters.get(arg, "")
        if val:
            try: amount = max(0, float(val))
            except ValueError: amount = None
            if amount is not None: clauses.append(f"amount {op} ?"); values.append(amount)
    search = filters.get("q", "").strip()[:100]
    if search:
        clauses.append("(description LIKE ? OR account_label LIKE ? OR category LIKE ?)")
        values.extend([f"%{search}%"] * 3)
    query = "SELECT * FROM transactions" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY date DESC,id DESC"
    with connect() as con: return [dict(r) for r in con.execute(query, values)]


def scoped_rows():
    scope = request.args.get("scope", "combined")
    rows = filtered()
    bank = request.args.get("bank", "")
    # Apply scope independently from optional UI bank filter.
    if scope == "single" and bank:
        rows = [r for r in rows if r["bank_name"] == bank]
    elif scope == "single" and not bank:
        rows = []
    elif scope == "compare":
        pass
    elif scope == "individual":
        pass
    return scope, rows


@app.context_processor
def inject_choices():
    dataset = request.args.get("dataset", "csv")
    if dataset not in {"csv", "pdf"}:
        dataset = "csv"
    with connect() as con:
        banks = [r[0] for r in con.execute("SELECT DISTINCT bank_name FROM transactions WHERE dataset=? ORDER BY bank_name", (dataset,))]
        currencies = [r[0] for r in con.execute("SELECT DISTINCT currency FROM transactions WHERE dataset=? ORDER BY currency", (dataset,))]
        categories = [r[0] for r in con.execute("SELECT DISTINCT category FROM transactions WHERE dataset=? ORDER BY category", (dataset,))]
    return {"banks": banks, "currencies": currencies, "categories": categories, "dataset": dataset}


def credit_dashboard_data():
    with connect() as con:
        scores = [dict(r) for r in con.execute(
            "SELECT * FROM credit_scores ORDER BY recorded_on DESC,id DESC")]
        loans = [dict(r) for r in con.execute("""SELECT l.*,u.outstanding_balance,u.emi_amount,
            u.interest_rate,u.next_due_date,u.status,u.note AS latest_note,u.updated_on
            FROM loans l LEFT JOIN loan_updates u ON u.id=(
              SELECT MAX(u2.id) FROM loan_updates u2 WHERE u2.loan_id=l.id)
            ORDER BY l.created_at DESC,l.id DESC""")]
        loan_updates = [dict(r) for r in con.execute("""SELECT u.*,l.lender,l.loan_type,l.currency
            FROM loan_updates u JOIN loans l ON l.id=u.loan_id
            ORDER BY u.updated_on DESC,u.id DESC LIMIT 50""")]
    return {"scores": scores, "loans": loans, "loan_updates": loan_updates}


@app.get("/")
def dashboard():
    scope, rows = scoped_rows()
    include_internal = request.args.get("include_internal") == "1"
    try: threshold = max(0, float(request.args.get("threshold", 10000)))
    except (TypeError, ValueError): threshold = 10000
    try: frequency = max(1, int(request.args.get("frequency", 8)))
    except (TypeError, ValueError): frequency = 8
    result = analyze(rows, include_internal=include_internal, large_threshold=threshold, frequency_count=frequency)
    # Scope selectors have a distinct result view while respecting date/currency/type filters.
    if scope == "compare":
        pass
    elif scope == "individual":
        pass
    bank = request.args.get("bank", "")
    return render_template("dashboard.html", scope=scope, bank=bank, result=result, rows=rows,
                           include_internal=include_internal, filters=request.args, today=date.today().isoformat(),
                           **credit_dashboard_data())


@app.get("/transactions")
def transactions():
    rows = filtered()
    return render_template("transactions.html", rows=rows, filters=request.args)


def _number_field(name, label, *, minimum=0, maximum=None, required=True):
    raw = request.form.get(name, "").strip()
    if not raw and not required:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label}.")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label.title()} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label.title()} must be no more than {maximum}.")
    return value


def _form_date(name, label, *, default_today=False):
    raw = request.form.get(name, "").strip()
    if not raw and default_today:
        return date.today().isoformat()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError(f"Choose a valid {label}.")


def _loan_status():
    status = request.form.get("status", "Active").strip().title()
    if status not in {"Active", "Paid", "Closed", "Overdue"}:
        raise ValueError("Choose Active, Paid, Closed, or Overdue for loan status.")
    return status


def _save_loan_update(con, loan_id):
    balance = _number_field("outstanding_balance", "outstanding balance")
    emi = _number_field("emi_amount", "monthly EMI")
    rate = _number_field("interest_rate", "interest rate", maximum=100, required=False)
    con.execute("""INSERT INTO loan_updates
        (loan_id,outstanding_balance,emi_amount,interest_rate,next_due_date,status,note,updated_on)
        VALUES(?,?,?,?,?,?,?,?)""",
        (loan_id, round(balance, 2), round(emi, 2), rate,
         _form_date("next_due_date", "next EMI date"), _loan_status(),
         request.form.get("note", "").strip()[:500], _form_date("updated_on", "update date", default_today=True)))


@app.get("/credit")
def credit_and_loans():
    return redirect(url_for("dashboard") + "#credit-loans")


@app.post("/credit/scores")
def add_credit_score():
    try:
        raw_score = request.form.get("score", "").strip()
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            raise ValueError("Enter a whole-number CIBIL score from 300 to 900.")
        if str(score) != raw_score or not 300 <= score <= 900:
            raise ValueError("CIBIL score must be a whole number from 300 to 900.")
        recorded_on = _form_date("recorded_on", "score date", default_today=True)
        source = request.form.get("source", "CIBIL").strip()[:100] or "CIBIL"
        notes = request.form.get("notes", "").strip()[:500]
        with connect() as con:
            con.execute("INSERT INTO credit_scores(score,recorded_on,source,notes) VALUES(?,?,?,?)",
                        (score, recorded_on, source, notes))
        flash("Credit score update saved.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "Enter a valid credit score.", "danger")
    return redirect(url_for("credit_and_loans"))


@app.post("/credit/loans")
def add_loan():
    try:
        lender = request.form.get("lender", "").strip()[:100]
        loan_type = request.form.get("loan_type", "").strip()[:80]
        currency = request.form.get("currency", "INR").strip().upper()
        original_amount = _number_field("original_amount", "original loan amount", minimum=0.01)
        if not lender or not loan_type:
            raise ValueError("Enter the lender and loan type.")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Currency must be a three-letter code, such as INR.")
        with connect() as con:
            cur = con.execute("INSERT INTO loans(lender,loan_type,original_amount,currency) VALUES(?,?,?,?)",
                              (lender, loan_type, round(original_amount, 2), currency))
            _save_loan_update(con, cur.lastrowid)
        flash("Loan and its first update were saved.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "Check the loan details and try again.", "danger")
    return redirect(url_for("credit_and_loans"))


@app.post("/credit/loans/<int:loan_id>/updates")
def add_loan_update(loan_id):
    try:
        with connect() as con:
            exists = con.execute("SELECT 1 FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not exists:
                return "Loan not found", 404
            _save_loan_update(con, loan_id)
        flash("Loan update saved to its history.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "Check the loan update and try again.", "danger")
    return redirect(url_for("credit_and_loans"))


def extract_statement_pdf(raw, pdf_password=""):
    """Extract a selectable-text transaction table from a bank statement PDF."""
    if pdfplumber is None:
        raise ValueError("PDF support is not installed. Run: python -m pip install pdfplumber")
    try:
        # The password is supplied for this parsing operation only. Never log or persist it.
        with pdfplumber.open(io.BytesIO(raw), password=pdf_password or "") as pdf:
            tables = []
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    clean = [[(cell or "").replace("\n", " ").strip() for cell in row] for row in table if row]
                    if clean:
                        tables.append(clean)
            if not tables:
                raise ValueError("No selectable transaction table was found. This may be a scanned PDF or an unsupported layout; use your bank's CSV export if available.")
    except ValueError:
        raise
    except Exception as exc:
        if pdf_password:
            raise ValueError("Could not open this PDF. Check its PDF-opening password. Do not enter your banking login, UPI PIN, or OTP.") from exc
        raise ValueError("Could not read this PDF. If it requires a PDF-opening password, enter it in the import form. Do not enter a banking login, UPI PIN, or OTP.") from exc

    def looks_like_header(row):
        text = " ".join(row).lower()
        has_date = "date" in text
        has_detail = any(word in text for word in ("description", "narration", "particular", "details", "transaction"))
        has_value = any(word in text for word in ("amount", "debit", "credit", "withdraw", "deposit", "paid", "received"))
        return has_date and has_value and (has_detail or len(row) >= 4)

    selected = None
    for table_index, table in enumerate(tables):
        for row_index, row in enumerate(table):
            if looks_like_header(row):
                selected = (table_index, row_index, row)
                break
        if selected:
            break
    if not selected:
        raise ValueError("Could not identify the PDF transaction table headings. Use a statement with a text table or export a CSV from your bank.")

    table_index, row_index, raw_header = selected
    header = []
    for i, value in enumerate(raw_header):
        label = value.strip() or f"Column {i + 1}"
        if label in header:
            label = f"{label} {i + 1}"
        header.append(label)
    records = []
    for ti, table in enumerate(tables):
        start = row_index + 1 if ti == table_index else 0
        for row in table[start:]:
            values = [(cell or "").replace("\n", " ").strip() for cell in row]
            if not any(values) or looks_like_header(values):
                continue
            if len(values) < len(header):
                values += [""] * (len(header) - len(values))
            records.append(values[:len(header)])
    if not records:
        raise ValueError("The PDF headings were found, but no transaction rows were extracted.")
    return pd.DataFrame(records, columns=header)


def read_statement_file(raw, extension, pdf_password=""):
    if extension == ".csv":
        text = None
        for encoding in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("CSV encoding was not recognized. Save/export it as UTF-8 CSV and try again.")
        sample = text[:8192]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
        try:
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False,
                             sep=delimiter, skipinitialspace=True)
        except Exception as exc:
            raise ValueError("CSV columns could not be read. Re-export it as a comma-separated CSV and try again.") from exc
        if len(df.columns) < 2:
            raise ValueError("Only one CSV column was detected. Check the delimiter or re-export the bank statement as CSV.")
        return df
    if extension == ".pdf":
        return extract_statement_pdf(raw, pdf_password)
    raise ValueError("Choose a PDF or CSV statement.")


def apply_pdf_categories(rows):
    """Apply requested merchant rules to PDF rows; leave other imports unchanged."""
    for row in rows:
        description = row["description"].casefold()
        if row["type"] != "debit":
            continue
        if re.search(r"\batm\b", description):
            row["category"] = "Withdrawal"
        elif "apsrtc" in description:
            row["category"] = "Transport"
        elif "zerodha" in description:
            row["category"] = "Investing"
        elif row["amount"] < 50:
            row["category"] = "Groceries"


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET": return render_template("upload.html", mapping=None)
    file = request.files.get("file")
    extension = os.path.splitext(secure_filename(file.filename).lower())[1] if file and file.filename else ""
    if not file or extension not in (".csv", ".pdf"):
        flash("Choose a PDF or CSV statement file.", "danger"); return redirect(url_for("upload"))
    try:
        raw = file.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024: raise ValueError("File exceeds the 8 MB limit")
        pdf_password = request.form.get("pdf_password", "") if extension == ".pdf" else ""
        df = read_statement_file(raw, extension, pdf_password)
        if df.empty: raise ValueError("The statement has no transaction rows")
        suggested = suggest_mapping(df.columns)
        # First phase: preview columns and mappings; second phase: validate and import.
        if request.form.get("confirm") != "yes":
            return render_template("upload.html", mapping=suggested, columns=list(df.columns), filename=secure_filename(file.filename), preview=df.head(5).to_dict("records"), rows_count=len(df), is_pdf=extension == ".pdf")
        mapping = {key: request.form.get("map_"+key, "") for key in suggested}
        rows, rejected = normalize_rows(df, mapping, request.form.get("bank_default", "Unknown"), request.form.get("account_default", ""), request.form.get("currency_default", "INR"), request.form.get("date_order") == "dayfirst")
        dataset = "pdf" if extension == ".pdf" else "csv"
        if dataset == "pdf" and not rows:
            raise ValueError("No valid transaction rows were found in the PDF. The previously imported PDF data was kept.")
        if extension == ".pdf":
            apply_pdf_categories(rows)
        for row in rows:
            row["dataset"] = dataset
            if dataset == "pdf":
                row["fingerprint"] = "pdf:" + row["fingerprint"]
        accepted = duplicates = 0
        with connect() as con:
            # Keep the sample/CSV dataset as the site's default while replacing
            # only the previous PDF dataset when another PDF is uploaded.
            if extension == ".pdf" and rows:
                con.execute("DELETE FROM transactions WHERE dataset='pdf'")
            elif request.form.get("replace_transactions") == "yes":
                con.execute("DELETE FROM transactions WHERE dataset='csv'")
            for row in rows:
                cur = con.execute("INSERT OR IGNORE INTO transactions(date,description,amount,type,currency,bank_name,account_label,category,fingerprint,dataset) VALUES(:date,:description,:amount,:type,:currency,:bank_name,:account_label,:category,:fingerprint,:dataset)", row)
                if cur.rowcount: accepted += 1
                else: duplicates += 1
            report = {"rejected_rows": rejected}
            cur = con.execute("INSERT INTO imports(filename,accepted,rejected,duplicates,report_json) VALUES(?,?,?,?,?)",
                              (secure_filename(file.filename), accepted, len(rejected), duplicates, json.dumps(report)))
            import_id = cur.lastrowid
        flash(f"Import complete: {accepted} accepted, {duplicates} duplicates, {len(rejected)} rejected.", "success")
        return render_template("import_result.html", accepted=accepted, duplicates=duplicates, rejected=rejected, import_id=import_id, dataset=dataset)
    except Exception as exc:
        flash(f"Statement could not be processed: {exc}", "danger"); return redirect(url_for("upload"))


@app.post("/clear-pdf")
def clear_pdf_data():
    with connect() as con:
        con.execute("DELETE FROM transactions WHERE dataset='pdf'")
        con.execute("DELETE FROM imports WHERE lower(filename) LIKE '%.pdf'")
    flash("PDF statement data cleared. The default CSV data is shown.", "success")
    return redirect(url_for("dashboard"))


@app.get("/imports/<int:import_id>/validation.csv")
def validation_report(import_id):
    with connect() as con: item = con.execute("SELECT report_json FROM imports WHERE id=?", (import_id,)).fetchone()
    if not item: return "Import report not found", 404
    rejected = json.loads(item[0]).get("rejected_rows", [])
    out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=["row", "reason", "data"]); writer.writeheader(); writer.writerows(rejected)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=validation-{import_id}.csv"})


@app.get("/alerts")
def alerts():
    scope, rows = scoped_rows()
    try: threshold = max(0, float(request.args.get("threshold", 10000)))
    except (TypeError, ValueError): threshold = 10000
    try: frequency = max(1, int(request.args.get("frequency", 8)))
    except (TypeError, ValueError): frequency = 8
    result = analyze(rows, include_internal=request.args.get("include_internal") == "1", large_threshold=threshold, frequency_count=frequency)
    status_map = {r["id"]: r["review_status"] for r in rows}
    for alert in result["alerts"]: alert["review_status"] = status_map.get(alert["id"], "unreviewed")
    return render_template("alerts.html", alerts=result["alerts"], filters=request.args, scope=scope, bank=request.args.get("bank", ""))


@app.post("/alerts/<int:tx_id>/review")
def review_alert(tx_id):
    status = request.form.get("status", "reviewed")
    if status not in ("unreviewed", "reviewed", "dismissed"): return "Invalid review status", 400
    with connect() as con: con.execute("UPDATE transactions SET review_status=? WHERE id=?", (status, tx_id))
    return redirect(request.referrer or url_for("alerts"))


@app.get("/export.csv")
def export():
    scope, rows = scoped_rows()
    out = io.StringIO(); cols = ["id", "date", "bank_name", "account_label", "description", "type", "amount", "currency", "category", "review_status"]
    writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=transactions-{scope}.csv"})


@app.get("/health")
def health(): return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    phone_mode = "--phone" in sys.argv[1:]
    if phone_mode:
        access_code = os.environ.get("PHONE_ACCESS_CODE", "")
        if len(access_code) < 12:
            raise SystemExit("Set PHONE_ACCESS_CODE to a private phrase of at least 12 characters before using --phone.")
        app.config["PHONE_MODE"] = True
        app.config["PHONE_ACCESS_CODE"] = access_code
        print("Phone upload mode: keep this on a private Wi-Fi network. On Android, open http://<this-computer-IPv4>:5000.")
        print("Find this computer's IPv4 address with ipconfig. Select Private networks only if Windows Firewall asks.")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        app.run(port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
