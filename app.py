from flask import Flask, render_template, request, redirect, url_for, flash
from datetime import datetime
import re
import sqlite3
import smtplib
from email.mime.text import MIMEText
from threading import Thread
from contextlib import closing

app = Flask(__name__)
app.secret_key = 'e2b7c6df1a0d421fa3a24a4f5c3bd7085d1cdbf2e1867e32e2c7cb25fa1c61c0' # Change this to a secure key in production

# --- DATABASE SETUP ---
DB_NAME = 'budget.db'

def get_db_connection():
    """Get a database connection with row factory"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # Allows dictionary-style access
    return conn

def init_db():
    """Initialize database with proper schema"""
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Transactions table
        c.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('received', 'sent', 'paid')),
                recipient TEXT,
                description TEXT,
                date TEXT NOT NULL,
                balance REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # User settings table
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                target_budget REAL CHECK(target_budget >= 0),
                email TEXT CHECK(email LIKE '%_@__%.__%'),
                email_notifications INTEGER DEFAULT 1 CHECK(email_notifications IN (0, 1)),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create default settings if none exist
        c.execute('''
            INSERT OR IGNORE INTO user_settings (id) VALUES (1)
        ''')
        
        conn.commit()

def check_db_schema():
    """Verify the database schema is up-to-date"""
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Check if created_at column exists in transactions
        c.execute("PRAGMA table_info(transactions)")
        columns = [col[1] for col in c.fetchall()]
        if 'created_at' not in columns:
            c.execute("ALTER TABLE transactions ADD COLUMN created_at TIMESTAMP")
            c.execute("UPDATE transactions SET created_at = CURRENT_TIMESTAMP")
        
        # Check if updated_at column exists in user_settings
        c.execute("PRAGMA table_info(user_settings)")
        columns = [col[1] for col in c.fetchall()]
        if 'updated_at' not in columns:
            c.execute("ALTER TABLE user_settings ADD COLUMN updated_at TIMESTAMP")
            c.execute("UPDATE user_settings SET updated_at = CURRENT_TIMESTAMP")
        
        conn.commit()

# --- PARSER FUNCTION ---
def parse_mpesa_sms(sms):
    """Parse M-PESA SMS messages into transaction data"""
    patterns = [
        # Received money
        r"received Ksh([\d,]+\.\d{2}) from (.+?) (\d{10}).+?on (\d{1,2}/\d{1,2}/\d{2}) at (\d{1,2}:\d{2} [APM]{2}).+?balance is Ksh([\d,]+\.\d{2})",
        # Sent money to paybill
        r"Confirmed\. Ksh([\d,]+\.\d{2}) sent to (.+?) (?:Paybill|paybill) (.+?) for account (\d+).+?on (\d{1,2}/\d{1,2}/\d{2}) at (\d{1,2}:\d{2} [APM]{2}).+?balance is Ksh([\d,]+\.\d{2})",
        # Sent money to phone
        r"Confirmed\. Ksh([\d,]+\.\d{2}) sent to (.+?) (\d{10}).+?on (\d{1,2}/\d{1,2}/\d{2}) at (\d{1,2}:\d{2} [APM]{2}).+?balance is Ksh([\d,]+\.\d{2})",
        # Paid to business
        r"paid to (.+?) (\d{10}) Ksh([\d,]+\.\d{2}).+?on (\d{1,2}/\d{1,2}/\d{2}) at (\d{1,2}:\d{2} [APM]{2}).+?balance is Ksh([\d,]+\.\d{2})"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, sms, re.IGNORECASE)
        if match:
            if "received" in sms.lower():
                return {
                    'amount': float(match.group(1).replace(',', '')),
                    'type': 'received',
                    'recipient': match.group(2),
                    'description': 'M-PESA Received',
                    'date': parse_date(f"{match.group(4)} {match.group(5)}"),
                    'balance': float(match.group(6).replace(',', ''))
                }
            elif "sent to" in sms.lower() and "paybill" in sms.lower():
                return {
                    'amount': float(match.group(1).replace(',', '')),
                    'type': 'paid',
                    'recipient': f"{match.group(2)} Paybill {match.group(3)}",
                    'description': f"Payment to {match.group(2)} (Account: {match.group(4)})",
                    'date': parse_date(f"{match.group(5)} {match.group(6)}"),
                    'balance': float(match.group(7).replace(',', ''))
                }
            elif "sent to" in sms.lower():
                return {
                    'amount': float(match.group(1).replace(',', '')),
                    'type': 'sent',
                    'recipient': match.group(2),
                    'description': 'M-PESA Sent',
                    'date': parse_date(f"{match.group(4)} {match.group(5)}"),
                    'balance': float(match.group(6).replace(',', ''))
                }
            elif "paid to" in sms.lower():
                return {
                    'amount': float(match.group(3).replace(',', '')),
                    'type': 'paid',
                    'recipient': match.group(1),
                    'description': 'M-PESA Payment',
                    'date': parse_date(f"{match.group(4)} {match.group(5)}"),
                    'balance': float(match.group(6).replace(',', ''))
                }
    return None

def parse_date(date_str):
    """Parse date string from M-PESA message"""
    try:
        return datetime.strptime(date_str, "%d/%m/%y %I:%M %p").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# --- EMAIL FUNCTION ---
def send_email_async(app, recipient, subject, body):
    """Send email notification in background thread"""
    with app.app_context():
        try:
            # Configure these with your email provider's settings
            sender_email = "wahhajsiraj16@gmail.com"
            sender_password = "hqoi cvpm yvyr zbkh"
            
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = sender_email
            msg['To'] = recipient
            
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        except Exception as e:
            app.logger.error(f"Failed to send email: {e}")

def check_budget_and_notify(app, current_balance):
    """Check if balance is below target and send notification if needed"""
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT target_budget, email, email_notifications 
            FROM user_settings 
            WHERE id = 1
        """)
        settings = c.fetchone()
    
    if settings and settings['email_notifications'] and settings['target_budget'] and current_balance < settings['target_budget']:
        subject = "Budget Alert: Your balance is below target!"
        body = f"""Hello,

Your current balance (Ksh {current_balance:,.2f}) has dropped below your target budget (Ksh {settings['target_budget']:,.2f}).

Please review your spending or adjust your target budget.

Regards,
M-Smart Budget Team
        """
        Thread(target=send_email_async, args=(app, settings['email'], subject, body)).start()

# --- ROUTES ---
@app.route('/')
def index():
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Get transactions
        c.execute("""
            SELECT *, datetime(date) as formatted_date 
            FROM transactions 
            ORDER BY datetime(date) DESC
        """)
        transactions = c.fetchall()
        
        # Get current balance (latest transaction balance)
        current_balance = 0
        if transactions:
            current_balance = transactions[0]['balance']
        
        # Get target budget
        c.execute("SELECT target_budget FROM user_settings WHERE id = 1")
        target_budget = c.fetchone()
        target_budget = target_budget['target_budget'] if target_budget else None
    
    return render_template('index.html', 
                         transactions=transactions,
                         current_balance=current_balance,
                         target_budget=target_budget)

@app.route('/parse', methods=['POST'])
def parse():
    sms = request.form['sms']
    parsed = parse_mpesa_sms(sms)

    if parsed:
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO transactions 
                (amount, type, recipient, description, date, balance)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                parsed['amount'], 
                parsed['type'], 
                parsed['recipient'], 
                parsed['description'], 
                parsed['date'], 
                parsed['balance']
            ))
            conn.commit()
            
            # Check if balance is below target budget
            check_budget_and_notify(app, parsed['balance'])
            
            flash('Transaction saved successfully!', 'success')
    else:
        flash('Could not parse the M-PESA message. Please check the format.', 'danger')
    
    return redirect(url_for('index'))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        target_budget = request.form.get('target_budget', type=float)
        email = request.form.get('email', '').strip()
        notifications = 1 if request.form.get('notifications') == 'on' else 0
        
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            c.execute('''
                UPDATE user_settings 
                SET target_budget = ?, email = ?, email_notifications = ?
                WHERE id = 1
            ''', (target_budget, email if email else None, notifications))
            conn.commit()
        
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('index'))
    
    # GET request - show current settings
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT target_budget, email, email_notifications 
            FROM user_settings 
            WHERE id = 1
        """)
        settings = c.fetchone()
    
    return render_template('settings.html', 
                         target_budget=settings['target_budget'] if settings['target_budget'] else None,
                         email=settings['email'] or '',
                         notifications=bool(settings['email_notifications']))

if __name__ == '__main__':
    init_db()
    check_db_schema()
    app.run(debug=True)