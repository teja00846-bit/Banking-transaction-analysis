"""Currency-aware, explainable transaction analysis helpers."""
from collections import defaultdict
from datetime import timedelta
import hashlib
import re

import pandas as pd


ALIASES = {
    "date": ["date", "transaction date", "txn date", "posted date", "value date"],
    "description": ["description", "details", "transaction details", "narration", "merchant", "payee", "particulars"],
    "amount": ["amount", "transaction amount", "value"],
    "type": ["type", "transaction type", "dr/cr", "debit/credit"],
    "debit": ["debit", "withdrawal", "withdrawals", "money out"],
    "credit": ["credit", "deposit", "deposits", "money in"],
    "currency": ["currency", "ccy"],
    "bank_name": ["bank", "bank name", "institution"],
    "account_label": ["account", "account name", "account label", "account id"],
    "category": ["category", "transaction category"],
}


def suggest_mapping(columns):
    normalized = {str(c).strip().lower(): c for c in columns}
    return {key: next((normalized[a] for a in aliases if a in normalized), "")
            for key, aliases in ALIASES.items()}


def normalize_rows(df, mapping, bank_default="Unknown", account_default="", currency_default="USD", dayfirst=False):
    """Return (valid rows, rejected rows) without silently dropping input."""
    rows, rejected = [], []
    for index, source in df.iterrows():
        rownum = int(index) + 2
        try:
            get = lambda key: source.get(mapping.get(key, "")) if mapping.get(key) else None
            date = pd.to_datetime(get("date"), errors="raise", dayfirst=dayfirst).date().isoformat()
            description = str(get("description") or "Unspecified").strip()
            currency = str(get("currency") or currency_default or "USD").strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("currency must be a three-letter code (for example USD)")
            debit, credit, amount = get("debit"), get("credit"), get("amount")
            def number(value):
                if value is None or not str(value).strip(): return 0.0
                raw = str(value).strip()
                negative = raw.startswith("(") and raw.endswith(")")
                cleaned = re.sub(r"[^0-9.+-]", "", raw.replace(",", ""))
                if cleaned in ("", "+", "-"): raise ValueError("amount must be numeric")
                parsed = float(cleaned)
                return -abs(parsed) if negative else parsed
            if mapping.get("debit") or mapping.get("credit"):
                d = number(debit)
                c = number(credit)
                if pd.isna(d) or pd.isna(c) or (float(d) and float(c)):
                    raise ValueError("debit/credit must contain one valid non-zero side")
                typ, value = ("debit", abs(float(d))) if float(d) else ("credit", abs(float(c)))
            else:
                value = number(amount)
                if value == 0:
                    raise ValueError("amount must be a non-zero number")
                typ_raw = str(get("type") or "").strip().lower()
                if typ_raw:
                    typ = "debit" if any(x in typ_raw for x in ("debit", "withdraw", "dr", "out")) else "credit" if any(x in typ_raw for x in ("credit", "deposit", "cr", "in")) else ""
                    if not typ: raise ValueError("type must identify credit or debit")
                    value = abs(float(value))
                else:
                    typ = "debit" if float(value) < 0 else "credit"
                    value = abs(float(value))
            bank = str(get("bank_name") or bank_default or "Unknown").strip() or "Unknown"
            account = str(get("account_label") or account_default or "").strip()
            category = str(get("category") or "Uncategorized").strip() or "Uncategorized"
            row = {"date": date, "description": description[:250], "amount": round(value, 2), "type": typ,
                   "currency": currency, "bank_name": bank[:100], "account_label": account[:100], "category": category[:100]}
            row["fingerprint"] = fingerprint(row)
            rows.append(row)
        except Exception as exc:
            rejected.append({"row": rownum, "reason": str(exc), "data": " | ".join(str(x) for x in source.tolist())[:500]})
    return rows, rejected


def fingerprint(row):
    parts = [row.get(k, "") for k in ("date", "bank_name", "account_label", "amount", "currency", "type", "description")]
    return hashlib.sha256("|".join(str(x).strip().lower() for x in parts).encode()).hexdigest()


def analyze(transactions, include_internal=False, large_threshold=10000, frequency_count=8):
    """Compute per-currency summaries and heuristic flags for a list of rows."""
    df = pd.DataFrame(transactions)
    if df.empty:
        return {"currencies": {}, "internal_ids": set(), "alerts": [], "monthly": {}, "categories": {}, "banks": {}, "periods": {}, "merchants": {}}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["currency"] = df["currency"].fillna("USD")
    # Pair opposite entries only when both descriptions explicitly suggest transfers.
    internal_ids = set()
    for currency, group in df.groupby("currency"):
        credits = group[group.type == "credit"]
        debits = group[group.type == "debit"]
        for _, d in debits.iterrows():
            if not re.search(r"transfer|xfer|own account|between accounts", str(d.description), re.I): continue
            candidates = credits[(credits.amount == d.amount) & (credits.date.sub(d.date).abs() <= pd.Timedelta(days=1)) & (credits.id != d.id)]
            for _, c in candidates.iterrows():
                if c.bank_name != d.bank_name or c.account_label != d.account_label:
                    internal_ids.update([int(d.id), int(c.id)]); break
    work = df if include_internal else df[~df.id.isin(internal_ids)]
    summaries, monthly, categories, banks, periods, merchants = {}, {}, {}, {}, {}, {}
    for currency, source_group in df.groupby("currency"):
        group = source_group if include_internal else source_group[~source_group.id.isin(internal_ids)]
        credits, debits = group[group.type == "credit"], group[group.type == "debit"]
        summaries[currency] = {"received": round(credits.amount.sum(), 2), "spent": round(debits.amount.sum(), 2),
            "net": round(credits.amount.sum()-debits.amount.sum(), 2), "count": int(len(group)),
            "average": round(group.amount.mean(), 2) if len(group) else 0,
            "largest_credit": round(credits.amount.max(),2) if len(credits) else 0,
            "largest_debit": round(debits.amount.max(),2) if len(debits) else 0,
            "possible_internal_count": int(group.id.isin(internal_ids).sum())}
        monthly[currency] = group.assign(month=group.date.dt.strftime("%Y-%m")).groupby(["month","type"]).amount.sum().unstack(fill_value=0).round(2).to_dict(orient="index")
        categories[currency] = debits.groupby("category").amount.sum().sort_values(ascending=False).round(2).to_dict()
        periods[currency] = {
            "daily": group.assign(period=group.date.dt.strftime("%Y-%m-%d")).groupby(["period","type"]).amount.sum().unstack(fill_value=0).round(2).to_dict(orient="index"),
            "weekly": group.assign(period=group.date.dt.to_period("W").astype(str)).groupby(["period","type"]).amount.sum().unstack(fill_value=0).round(2).to_dict(orient="index"),
            "yearly": group.assign(period=group.date.dt.strftime("%Y")).groupby(["period","type"]).amount.sum().unstack(fill_value=0).round(2).to_dict(orient="index"),
        }
        merchants[currency] = debits.groupby("description").amount.sum().sort_values(ascending=False).round(2).head(20).to_dict()
        bankout = {}
        for bank, bg in group.groupby("bank_name"):
            cr, db = bg[bg.type=="credit"].amount.sum(), bg[bg.type=="debit"].amount.sum()
            bankout[bank] = {"received":round(cr,2),"spent":round(db,2),"net":round(cr-db,2),"count":int(len(bg)),"average":round(bg.amount.mean(),2) if len(bg) else 0}
        banks[currency] = bankout
    alerts = []
    for _, r in df.iterrows():
        reasons=[]
        if r.amount >= float(large_threshold): reasons.append(f"Amount is at or above the configured {large_threshold:g} {r.currency} threshold")
        if int(r.id) in internal_ids: reasons.append("Possible internal transfer: a matching opposite entry was found within one day across a different bank or account; verify both entries")
        same = df[(df.bank_name==r.bank_name)&(df.account_label==r.account_label)&(df.currency==r.currency)&(df.amount==r.amount)&(df.date==r.date)]
        if len(same)>1: reasons.append("Repeated same-day amount in the same bank/account/currency; review for a duplicate")
        short = df[(df.bank_name==r.bank_name)&(df.account_label==r.account_label)&(df.date==r.date)&(df.currency==r.currency)]
        if len(short)>=int(frequency_count): reasons.append(f"High activity: {len(short)} transactions for this account on this date (limit {frequency_count})")
        if reasons:
            alerts.append({"id":int(r.id),"date":r.date.strftime("%Y-%m-%d"),"bank_name":r.bank_name,"account_label":r.account_label,
                "amount":float(r.amount),"currency":r.currency,"description":r.description,"risk_score":min(95,30+20*len(reasons)),
                "rules":reasons,"explanation":"Heuristic review signal only; this does not establish fraud."})
    return {"currencies":summaries,"internal_ids":internal_ids,"alerts":alerts,"monthly":monthly,"categories":categories,"banks":banks,"periods":periods,"merchants":merchants}
