# Finance Khichidi: Banking Transaction Analysis

A local Flask application for analyzing bank transactions. It can import PDF statements with selectable text tables or CSV files, preview and map columns, validate entries, and store accepted transactions in SQLite. It does not connect to a bank or scan phone storage.

## Start on this computer

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000/ on the same computer. The default dashboard uses the bundled synthetic `sample_transactions.csv` dataset. Your PDF imports are stored separately and can be viewed without replacing the default CSV dataset.

## Upload a statement PDF from Android

Keep the computer and phone on the same trusted private Wi-Fi network. On the computer, in PowerShell, run:

```powershell
$env:PHONE_ACCESS_CODE = "choose-your-own-private-phrase-at-least-12-characters"
python app.py --phone
```

Leave the server running. If Windows Firewall asks, allow access on **Private networks only**. Run `ipconfig` and find the computer's IPv4 address for its Wi-Fi adapter. On Android, open Chrome and visit `http://<computer-IPv4>:5000` (replace the placeholder with that address). Enter the access phrase, open **Import Statement**, choose the bank statement PDF, review the preview and mappings, then confirm the import.

The phone's file picker only uploads the file you select. It does not automatically read the phone or connect to your bank. Phone mode is passphrase protected and intended only for a trusted private network; stop it with Ctrl+C when finished. Never enter bank login passwords, PINs, or OTPs.

PDF extraction supports selectable-text transaction tables. For a protected PDF, enter its **PDF-opening password** in the import form; the app uses it only to parse the uploaded file and does not save it. The confirmation step asks you to select the file and enter the PDF password again. Do not enter bank login passwords, UPI PINs, or OTPs. Scanned/image PDFs are not supported. If the columns cannot be extracted reliably, use your bank's CSV export. Values are kept separate by currency; when a statement has no currency column, choose the appropriate default currency in the import form (INR by default). A PDF import is shown in its own analysis view; the site's normal/default page continues to show the CSV dataset. Use **Clear PDF data and return to default CSV** to permanently remove imported PDF transactions and PDF import reports. CSV imports can opt to replace existing CSV transactions with the checkbox in the confirmation form. CIBIL and loan records are unaffected.

PDF imports also apply simple description-based categories to debit transactions: ATM entries become **Withdrawal**, APSRTC becomes **Transport**, Zerodha becomes **Investing**, and other debits below ₹50 become **Groceries**. These rules do not change CSV imports; other PDF rows keep the category from the statement or appear as Uncategorized.

## What the app does

- Four scopes: all banks combined, one bank, compare banks, or separate bank summaries.
- Currency-aware income, spending, cash flow, period summaries, categories, and merchant descriptions.
- Date, bank, account, currency, type, category, text, and amount filtering, plus CSV export.
- Duplicate skipping and possible internal-transfer identification.
- Explainable rule-based alerts with review status. Alerts are prompts for review, not confirmed fraud.
- Manual CIBIL score history and loan balance, EMI, rate, due-date, and payment-status updates. These are stored locally and do not sync with CIBIL or lenders.

## Tests

```powershell
python -m pytest -q
```

## Privacy

The app stores accepted transactions in `instance/banking.db` on the computer running Flask. Do not send that database or real statement PDFs to teammates unless you intend to share the financial information in them. Each teammate can run their own local copy and import their own statement. This is a local educational prototype, not a production banking service.
