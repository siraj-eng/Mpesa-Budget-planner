from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from datetime import datetime
import re
import sqlite3
import smtplib
from email.mime.text import MIMEText
from threading import Thread
from contextlib import closing
from werkzeug.security import generate_password_hash

app = Flask(__name__)
app.secret_key = 'e2b7c6df1a0d421fa3a24a4f5c3bd7085d1cdbf2e1867e32e2c7cb25fa1c61c0'

# --- DATABASE SETUP ---
DB_NAME = 'budget.db'

def get_db_connection():
    """Get a database connection with row factory"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database with proper schema"""
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Categories table (MISSING IN ORIGINAL)
        c.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL DEFAULT '#6c757d',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Budget goals table (MISSING IN ORIGINAL)
        c.execute('''
            CREATE TABLE IF NOT EXISTS budget_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                monthly_limit REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories (id)
            )
        ''')
        
        # Transaction rules table (MISSING IN ORIGINAL)
        c.execute('''
            CREATE TABLE IF NOT EXISTS transaction_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_match TEXT,
                pattern TEXT,
                category_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES categories (id)
            )
        ''')
        
        # Transactions table (UPDATED to include category_id)
        c.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('received', 'sent', 'paid')),
                recipient TEXT,
                description TEXT,
                date TEXT NOT NULL,
                balance REAL NOT NULL,
                category_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories (id)
            )
        ''')
        
        # User settings table
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                target_budget REAL CHECK(target_budget >= 0),
                email TEXT CHECK(email LIKE '%_@__%.__%'),
                email_notifications INTEGER DEFAULT 1 CHECK(email_notifications IN (0, 1)),
                password_hash TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Insert default categories
        default_categories = [
            ('Other', '#6c757d'),
            ('Food & Drinks', '#28a745'),
            ('Transport', '#007bff'),
            ('Shopping', '#ffc107'),
            ('Bills & Utilities', '#dc3545'),
            ('Entertainment', '#6f42c1'),
            ('Health', '#20c997'),
            ('Education', '#fd7e14')
        ]
        
        for name, color in default_categories:
            c.execute('INSERT OR IGNORE INTO categories (name, color) VALUES (?, ?)', (name, color))
        
        # Create default settings if none exist
        c.execute('INSERT OR IGNORE INTO user_settings (id) VALUES (1)')
        
        conn.commit()

def check_db_schema():
    """Verify the database schema is up-to-date"""
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Check if category_id column exists in transactions
        c.execute("PRAGMA table_info(transactions)")
        columns = [col[1] for col in c.fetchall()]
        if 'category_id' not in columns:
            c.execute("ALTER TABLE transactions ADD COLUMN category_id INTEGER")
            # Set default category for existing transactions
            c.execute("SELECT id FROM categories WHERE name = 'Other'")
            other_id = c.fetchone()
            if other_id:
                c.execute("UPDATE transactions SET category_id = ? WHERE category_id IS NULL", (other_id['id'],))
        
        # Check if created_at column exists in transactions
        if 'created_at' not in columns:
            c.execute("ALTER TABLE transactions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            c.execute("UPDATE transactions SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
        
        # Check if password_hash column exists in user_settings
        c.execute("PRAGMA table_info(user_settings)")
        columns = [col[1] for col in c.fetchall()]
        if 'password_hash' not in columns:
            c.execute("ALTER TABLE user_settings ADD COLUMN password_hash TEXT")
        
        if 'updated_at' not in columns:
            c.execute("ALTER TABLE user_settings ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            c.execute("UPDATE user_settings SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")
        
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

# ===== ROUTES =====
@app.route('/')
def index():
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Get transactions with category info
        c.execute('''
            SELECT t.*, c.name as category_name, c.color 
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            ORDER BY datetime(t.date) DESC
            LIMIT 50
        ''')
        transactions = c.fetchall()
        
        # Get current balance
        current_balance = 0
        if transactions:
            current_balance = transactions[0]['balance']
        
        # Get target budget
        c.execute("SELECT target_budget FROM user_settings WHERE id = 1")
        settings_row = c.fetchone()
        target_budget = settings_row['target_budget'] if settings_row and settings_row['target_budget'] else None
        
        # Get categories
        c.execute("SELECT * FROM categories ORDER BY name")
        categories = c.fetchall()
        
        # Get budget goals with spending data
        c.execute('''
            SELECT c.id, c.name, c.color, bg.monthly_limit,
                   COALESCE(SUM(CASE WHEN t.type IN ('paid', 'sent') 
                                    AND strftime('%Y-%m', t.date) = strftime('%Y-%m', 'now') 
                                    THEN t.amount ELSE 0 END), 0) as spent
            FROM categories c
            LEFT JOIN budget_goals bg ON c.id = bg.category_id
            LEFT JOIN transactions t ON t.category_id = c.id
            GROUP BY c.id, c.name, c.color, bg.monthly_limit
            HAVING bg.monthly_limit IS NOT NULL
        ''')
        budget_goals = c.fetchall()
    
    return render_template('index.html', 
                         transactions=transactions,
                         current_balance=current_balance,
                         target_budget=target_budget,
                         categories=categories,
                         budget_goals=budget_goals)

@app.route('/parse', methods=['POST'])
def parse():
    sms = request.form['sms']
    parsed = parse_mpesa_sms(sms)

    if parsed:
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            
            # Auto-categorize using rules
            c.execute('''
                SELECT category_id 
                FROM transaction_rules 
                WHERE ? LIKE recipient_match
                OR ? LIKE pattern
                LIMIT 1
            ''', (parsed['recipient'], parsed['description']))
            rule = c.fetchone()
            category_id = rule['category_id'] if rule else None
            
            if not category_id:
                # Default to 'Other' category if no rule matches
                c.execute("SELECT id FROM categories WHERE name = 'Other'")
                other_cat = c.fetchone()
                category_id = other_cat['id'] if other_cat else 1
            
            c.execute('''
                INSERT INTO transactions 
                (amount, type, recipient, description, date, balance, category_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                parsed['amount'], 
                parsed['type'], 
                parsed['recipient'], 
                parsed['description'], 
                parsed['date'], 
                parsed['balance'],
                category_id
            ))
            conn.commit()
            
            check_budget_and_notify(app, parsed['balance'])
            flash('Transaction saved successfully!', 'success')
    else:
        flash('Could not parse the M-PESA message. Please check the format.', 'danger')
    
    return redirect(url_for('index'))

@app.route('/add-transaction', methods=['POST'])
def add_transaction():
    try:
        amount = float(request.form['amount'])
        transaction_type = request.form['type']
        recipient = request.form.get('recipient', '')
        description = request.form.get('description', '')
        transaction_date = request.form.get('date', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        balance = float(request.form['balance'])
        category_id = request.form.get('category_id')
        
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO transactions 
                (amount, type, recipient, description, date, balance, category_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (amount, transaction_type, recipient, description, transaction_date, balance, category_id))
            conn.commit()
            
            check_budget_and_notify(app, balance)
            flash('Transaction added successfully!', 'success')
    except Exception as e:
        flash(f'Error adding transaction: {str(e)}', 'danger')
    
    return redirect(url_for('index'))

@app.route('/delete-transaction/<int:transaction_id>', methods=['POST'])
def delete_transaction(transaction_id):
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        conn.commit()
        flash('Transaction deleted successfully!', 'success')
    return redirect(url_for('index'))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        target_budget = request.form.get('target_budget', type=float)
        email = request.form.get('email', '').strip()
        notifications = 1 if request.form.get('notifications') == 'on' else 0
        new_password = request.form.get('password')
        
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            
            if new_password:
                c.execute('''
                    UPDATE user_settings 
                    SET target_budget = ?, email = ?, email_notifications = ?, password_hash = ?
                    WHERE id = 1
                ''', (target_budget, email if email else None, notifications, generate_password_hash(new_password)))
            else:
                c.execute('''
                    UPDATE user_settings 
                    SET target_budget = ?, email = ?, email_notifications = ?
                    WHERE id = 1
                ''', (target_budget, email if email else None, notifications))
            conn.commit()
        
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings'))
    
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM user_settings WHERE id = 1")
        settings = c.fetchone()
    
    return render_template('settings.html', 
                         target_budget=settings['target_budget'] if settings and settings['target_budget'] else None,
                         email=settings['email'] or '' if settings else '',
                         notifications=bool(settings['email_notifications']) if settings else False)

@app.route('/categories', methods=['GET', 'POST'])
def manage_categories():
    if request.method == 'POST':
        name = request.form['name'].strip()
        color = request.form['color']
        
        with closing(get_db_connection()) as conn:
            c = conn.cursor()
            try:
                c.execute('INSERT INTO categories (name, color) VALUES (?, ?)', (name, color))
                conn.commit()
                flash('Category added successfully!', 'success')
            except sqlite3.IntegrityError:
                flash('Category already exists!', 'danger')
        
        return redirect(url_for('manage_categories'))
    
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute('''
            SELECT c.*, COUNT(t.id) as transaction_count
            FROM categories c
            LEFT JOIN transactions t ON c.id = t.category_id
            GROUP BY c.id
            ORDER BY c.name
        ''')
        categories = c.fetchall()
    
    return render_template('categories.html', categories=categories)

@app.route('/delete-category/<int:id>', methods=['POST'])
def delete_category(id):
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        try:
            # First update transactions with this category to 'Other'
            c.execute("SELECT id FROM categories WHERE name = 'Other'")
            other_cat = c.fetchone()
            other_id = other_cat['id'] if other_cat else 1
            
            c.execute('''
                UPDATE transactions 
                SET category_id = ?
                WHERE category_id = ?
            ''', (other_id, id))
            
            # Delete budget goals for this category
            c.execute("DELETE FROM budget_goals WHERE category_id = ?", (id,))
            
            # Then delete the category
            c.execute("DELETE FROM categories WHERE id = ?", (id,))
            conn.commit()
            flash('Category deleted successfully!', 'success')
        except Exception as e:
            flash(f'Error deleting category: {str(e)}', 'danger')
    
    return redirect(url_for('manage_categories'))

@app.route('/set-budget-goal', methods=['POST'])
def set_budget_goal():
    category_id = request.form['category_id']
    monthly_limit = request.form.get('monthly_limit', type=float)
    
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT OR REPLACE INTO budget_goals (category_id, monthly_limit)
                VALUES (?, ?)
            ''', (category_id, monthly_limit))
            conn.commit()
            flash('Budget goal set successfully!', 'success')
        except Exception as e:
            flash(f'Error setting budget goal: {str(e)}', 'danger')
    
    return redirect(url_for('index'))

@app.route('/analytics')
def analytics():
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        
        # Spending by category
        c.execute('''
            SELECT c.name, c.color, COALESCE(SUM(t.amount), 0) as total 
            FROM categories c
            LEFT JOIN transactions t ON c.id = t.category_id AND t.type IN ('paid', 'sent')
            GROUP BY c.id, c.name, c.color
            HAVING total > 0
            ORDER BY total DESC
        ''')
        category_data = c.fetchall()
        
        # Monthly trends
        c.execute('''
            SELECT strftime('%Y-%m', date) as month,
                   SUM(CASE WHEN type = 'received' THEN amount ELSE 0 END) as income,
                   SUM(CASE WHEN type IN ('paid', 'sent') THEN amount ELSE 0 END) as expenses
            FROM transactions
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        ''')
        trend_data = c.fetchall()
        
        # Budget progress
        c.execute('''
            SELECT c.name, c.color, bg.monthly_limit, 
                   COALESCE(SUM(CASE WHEN t.type IN ('paid', 'sent') 
                                    AND strftime('%Y-%m', t.date) = strftime('%Y-%m', 'now')
                                    THEN t.amount ELSE 0 END), 0) as spent,
                   (bg.monthly_limit - COALESCE(SUM(CASE WHEN t.type IN ('paid', 'sent') 
                                                        AND strftime('%Y-%m', t.date) = strftime('%Y-%m', 'now')
                                                        THEN t.amount ELSE 0 END), 0)) as remaining
            FROM budget_goals bg
            JOIN categories c ON bg.category_id = c.id
            LEFT JOIN transactions t ON t.category_id = c.id
            GROUP BY c.id, c.name, c.color, bg.monthly_limit
        ''')
        budget_progress = c.fetchall()
    
    return render_template('analytics.html',
                         category_data=category_data,
                         trend_data=trend_data,
                         budget_progress=budget_progress)

@app.route('/api/transactions')
def api_transactions():
    limit = request.args.get('limit', default=50, type=int)
    with closing(get_db_connection()) as conn:
        c = conn.cursor()
        c.execute('''
            SELECT t.*, c.name as category_name, c.color 
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            ORDER BY datetime(t.date) DESC 
            LIMIT ?
        ''', (limit,))
        transactions = [dict(row) for row in c.fetchall()]
    return jsonify(transactions)

@app.route('/export-data')
def export_data():
    with closing(get_db_connection()) as conn:
        return Response(
            '\n'.join(conn.iterdump()),
            mimetype='application/sql',
            headers={'Content-Disposition': 'attachment;filename=budget_backup.sql'}
        )

if __name__ == '__main__':
    init_db()
    check_db_schema()
    app.run(debug=True)