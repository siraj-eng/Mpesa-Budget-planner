from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import re
import sqlite3

app = Flask(__name__)

# --- DATABASE SETUP ---
DB_NAME = 'budget.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL,
            type TEXT,
            recipient TEXT,
            description TEXT,
            date TEXT,
            balance REAL
        )
    ''')
    conn.commit()
    conn.close()

# --- PARSER FUNCTION ---
def parse_mpesa_sms(sms):
    # Sample M-PESA message structure
    pattern = r"received Ksh([\d,]+\.\d{2}) from (.+?) (\d{10}).+?on (\d{1,2}/\d{1,2}/\d{2}) at (\d{1,2}:\d{2} [APM]{2}).+?balance is Ksh([\d,]+\.\d{2})"
    match = re.search(pattern, sms, re.IGNORECASE)

    if match:
        amount = float(match.group(1).replace(',', ''))
        recipient = match.group(2)
        date_str = match.group(4) + ' ' + match.group(5)
        date_obj = datetime.strptime(date_str, "%d/%m/%y %I:%M %p")
        balance = float(match.group(6).replace(',', ''))

        return {
            'amount': amount,
            'type': 'received',
            'recipient': recipient,
            'description': 'M-PESA Received',
            'date': date_obj.strftime("%Y-%m-%d %H:%M:%S"),
            'balance': balance
        }
    return None

# --- ROUTES ---
@app.route('/')
def index():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM transactions ORDER BY date DESC")
    transactions = c.fetchall()
    conn.close()
    return render_template('index.html', transactions=transactions)

@app.route('/parse', methods=['POST'])
def parse():
    sms = request.form['sms']
    parsed = parse_mpesa_sms(sms)

    if parsed:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''
            INSERT INTO transactions (amount, type, recipient, description, date, balance)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (parsed['amount'], parsed['type'], parsed['recipient'], parsed['description'], parsed['date'], parsed['balance']))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
