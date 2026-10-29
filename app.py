"""
CareSlot - Hospital Appointment Management System (academic prototype)
Python 3 + Flask + SQLite.   Run:  python app.py   then open http://127.0.0.1:5000
"""
import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "hospital.db")
BOOKING_WINDOW_DAYS = 30
STATUSES = ["Booked", "Completed", "Cancelled", "Missed"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _secret_key():
    """Use SECRET_KEY env var, else create and persist a random key next to the app."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(BASE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # idle timeout (refreshed every request)
)

# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, phone TEXT NOT NULL,
  password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'patient' CHECK (role IN ('patient','admin')),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS departments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doctors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  department_id INTEGER NOT NULL REFERENCES departments(id),
  qualification TEXT DEFAULT '', experience_years INTEGER DEFAULT 0,
  phone TEXT DEFAULT '', email TEXT DEFAULT '',
  work_days TEXT NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri',
  start_time TEXT NOT NULL DEFAULT '09:00', end_time TEXT NOT NULL DEFAULT '13:00',
  slot_minutes INTEGER NOT NULL DEFAULT 20,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive'))
);
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doctor_id INTEGER NOT NULL REFERENCES doctors(id),
  appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
  reason TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'Booked' CHECK (status IN ('Booked','Completed','Cancelled','Missed')),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- hard guarantee against double booking: one non-cancelled appointment per doctor slot
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot
  ON appointments(doctor_id, appointment_date, appointment_time) WHERE status <> 'Cancelled';
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  channel TEXT NOT NULL, message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS activity_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, action TEXT NOT NULL, details TEXT DEFAULT '', ip TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

DEPARTMENTS = [
    ("General Medicine", "Primary care and common illnesses"),
    ("Cardiology", "Heart and blood vessel care"),
    ("Dentistry", "Dental and oral health"),
    ("Ophthalmology", "Eye care and vision"),
    ("Pediatrics", "Healthcare for children"),
    ("Orthopedics", "Bones, joints and muscles"),
]
# name, dept_id, qualification, exp, phone, email, days, start, end, slot_minutes
DOCTORS = [
    ("Dr. Anil Sharma", 1, "MBBS, MD", 14, "9100000001", "anil@hospital.test", "Mon,Tue,Wed,Thu,Fri", "09:00", "13:00", 20),
    ("Dr. Meera Iyer", 1, "MBBS, DNB", 9, "9100000002", "meera@hospital.test", "Mon,Wed,Fri,Sat", "14:00", "18:00", 20),
    ("Dr. Rohan Kulkarni", 2, "MBBS, DM Cardiology", 18, "9100000003", "rohan@hospital.test", "Mon,Tue,Thu,Fri", "10:00", "14:00", 30),
    ("Dr. Sneha Patil", 3, "BDS, MDS", 8, "9100000004", "sneha@hospital.test", "Mon,Tue,Wed,Thu,Fri,Sat", "09:30", "13:30", 30),
    ("Dr. Imran Shaikh", 4, "MBBS, MS Ophthalmology", 12, "9100000005", "imran@hospital.test", "Tue,Wed,Thu,Sat", "11:00", "15:00", 20),
    ("Dr. Kavita Deshmukh", 5, "MBBS, MD Pediatrics", 11, "9100000006", "kavita@hospital.test", "Mon,Tue,Wed,Thu,Fri", "09:00", "12:00", 15),
    ("Dr. Vikram Joshi", 6, "MBBS, MS Orthopedics", 16, "9100000007", "vikram@hospital.test", "Mon,Wed,Fri", "15:00", "19:00", 30),
]


def connect():
    db = sqlite3.connect(DATABASE, isolation_level=None)  # autocommit; we use explicit BEGIN where needed
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    db = connect()
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        db.execute("INSERT INTO users (name,email,phone,password,role) VALUES (?,?,?,?,?)",
                   ("Hospital Admin", "admin@hospital.test", "9000000001", generate_password_hash("Admin@123"), "admin"))
        db.execute("INSERT INTO users (name,email,phone,password,role) VALUES (?,?,?,?,?)",
                   ("Demo Patient", "patient@hospital.test", "9000000002", generate_password_hash("Patient@123"), "patient"))
        db.executemany("INSERT INTO departments (name,description) VALUES (?,?)", DEPARTMENTS)
        db.executemany("""INSERT INTO doctors (name,department_id,qualification,experience_years,phone,email,
                          work_days,start_time,end_time,slot_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)""", DOCTORS)
    db.close()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    return get_db().execute(sql, args)


# --------------------------------------------------------------------------- helpers
def current_user():
    return session.get("user")


def log_activity(action, details=""):
    u = current_user()
    ex("INSERT INTO activity_logs (user_id, action, details, ip) VALUES (?,?,?,?)",
       (u["id"] if u else None, action, details, request.remote_addr or ""))


def notify(user_id, message):
    """Prototype notification service: stores simulated Email + SMS messages in the database.
    Replace the body with smtplib / an SMS gateway call to send real messages."""
    for channel in ("Email", "SMS"):
        ex("INSERT INTO notifications (user_id, channel, message) VALUES (?,?,?)", (user_id, channel, message))


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            if role and u["role"] != role:
                if u["role"] == "admin":
                    return redirect(url_for("admin_dashboard"))
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def get_slots(doctor, date_str):
    """[{'time': '09:00', 'available': bool}, ...] for a doctor on a date."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []
    if d < date.today() or d.strftime("%a") not in doctor["work_days"].split(","):
        return []
    taken = {r["appointment_time"] for r in q(
        "SELECT appointment_time FROM appointments WHERE doctor_id=? AND appointment_date=? AND status<>'Cancelled'",
        (doctor["id"], date_str))}
    cur = datetime.combine(d, datetime.strptime(doctor["start_time"], "%H:%M").time())
    end = datetime.combine(d, datetime.strptime(doctor["end_time"], "%H:%M").time())
    step = timedelta(minutes=max(5, doctor["slot_minutes"]))
    now, slots = datetime.now(), []
    while cur + step <= end:
        t = cur.strftime("%H:%M")
        slots.append({"time": t, "available": t not in taken and cur > now})
        cur += step
    return slots


@app.template_filter("fmt_time")
def fmt_time(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p")


@app.template_filter("fmt_date")
def fmt_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a, %d %b %Y")


@app.template_filter("fmt_dt")
def fmt_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y, %I:%M %p")


@app.template_filter("badge")
def badge(status):
    return {"Booked": "blue", "Completed": "green", "Cancelled": "grey", "Missed": "red",
            "active": "green", "inactive": "grey"}.get(status, "grey")


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)
    return {"user": current_user(), "csrf_token": session["csrf"], "endpoint": request.endpoint}


@app.before_request
def protect():
    session.permanent = True
    if request.method == "POST":
        if not secrets.compare_digest(session.get("csrf", ""), request.form.get("csrf", "")):
            abort(400, "Invalid request token. Go back, refresh the page and try again.")


# --------------------------------------------------------------------------- public / auth
@app.route("/")
def index():
    depts = q("""SELECT d.*, (SELECT COUNT(*) FROM doctors WHERE department_id=d.id AND status='active') AS n
                 FROM departments d ORDER BY name""")
    doc_count = q("SELECT COUNT(*) c FROM doctors WHERE status='active'", one=True)["c"]
    return render_template("index.html", depts=depts, doc_count=doc_count)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    errors, v = [], {"name": "", "email": "", "phone": ""}
    if request.method == "POST":
        v = {k: request.form.get(k, "").strip() for k in v}
        v["email"] = v["email"].lower()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(v["name"]) < 2:
            errors.append("Please enter your full name.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
            errors.append("Enter a valid email address.")
        if not re.fullmatch(r"[0-9+\- ]{8,15}", v["phone"]):
            errors.append("Enter a valid phone number (8-15 digits).")
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
            errors.append("Password must be 8+ characters with letters and numbers.")
        if pw != pw2:
            errors.append("Passwords do not match.")
        if not errors and q("SELECT 1 FROM users WHERE email=?", (v["email"],), one=True):
            errors.append("An account with this email already exists.")
        if not errors:
            cur = ex("INSERT INTO users (name,email,phone,password,role) VALUES (?,?,?,?,'patient')",
                     (v["name"], v["email"], v["phone"], generate_password_hash(pw)))
            session.clear()
            session["user"] = {"id": cur.lastrowid, "name": v["name"], "email": v["email"], "role": "patient"}
            log_activity("register", v["email"])
            notify(cur.lastrowid, f"Welcome to CareSlot, {v['name']}! Your account is ready.")
            flash("Account created. Welcome!", "success")
            return redirect(url_for("book"))
    return render_template("register.html", errors=errors, v=v)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("admin_dashboard" if current_user()["role"] == "admin" else "index"))
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        now = datetime.now().timestamp()
        fails = [t for t in session.get("fails", []) if t > now - 600]
        if len(fails) >= 5:  # basic brute-force protection
            error = "Too many failed attempts. Please wait a few minutes."
        else:
            u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
            if u and check_password_hash(u["password"], pw):
                session.clear()
                session["user"] = {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}
                log_activity("login", u["role"])
                return redirect(url_for("admin_dashboard" if u["role"] == "admin" else "index"))
            fails.append(now)
            session["fails"] = fails
            log_activity("login_failed", email)
            error = "Incorrect email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    if current_user():
        log_activity("logout")
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- patient
@app.route("/doctors")
def doctors():
    qtext = request.args.get("q", "").strip()
    dept = request.args.get("department", type=int) or 0
    sql = """SELECT d.*, dp.name AS dept FROM doctors d JOIN departments dp ON dp.id=d.department_id
             WHERE d.status='active'"""
    args = []
    if dept:
        sql += " AND d.department_id=?"
        args.append(dept)
    if qtext:
        sql += " AND (d.name LIKE ? OR d.qualification LIKE ?)"
        args += [f"%{qtext}%"] * 2
    docs = q(sql + " ORDER BY d.name", args)
    return render_template("doctors.html", docs=docs, depts=q("SELECT id,name FROM departments ORDER BY name"),
                           qtext=qtext, dept=dept)


@app.route("/book", methods=["GET", "POST"])
@login_required("patient")
def book():
    user = current_user()
    min_date = date.today().isoformat()
    max_date = (date.today() + timedelta(days=BOOKING_WINDOW_DAYS)).isoformat()

    if request.method == "POST":
        doc_id = request.form.get("doctor_id", type=int) or 0
        d = request.form.get("date", "")
        t = request.form.get("time", "")
        reason = request.form.get("reason", "").strip()[:250]
        back = url_for("book", department=request.form.get("department", 0), doctor=doc_id, date=d)

        doc = q("SELECT * FROM doctors WHERE id=? AND status='active'", (doc_id,), one=True)
        ok = bool(doc) and min_date <= d <= max_date and any(
            s["time"] == t and s["available"] for s in get_slots(doc, d))
        if not ok:
            flash("That slot is not available. Please choose another.", "error")
            return redirect(back)

        db = get_db()
        db.execute("BEGIN IMMEDIATE")  # take the write lock so two people cannot grab the same slot
        try:
            if db.execute("""SELECT 1 FROM appointments WHERE doctor_id=? AND appointment_date=?
                             AND appointment_time=? AND status<>'Cancelled'""", (doc_id, d, t)).fetchone():
                raise ValueError("Sorry, that slot was just taken. Please choose another.")
            if db.execute("""SELECT 1 FROM appointments WHERE patient_id=? AND doctor_id=? AND appointment_date=?
                             AND status='Booked'""", (user["id"], doc_id, d)).fetchone():
                raise ValueError("You already have an appointment with this doctor on that day.")
            appt_id = db.execute("""INSERT INTO appointments (patient_id,doctor_id,appointment_date,appointment_time,reason)
                                    VALUES (?,?,?,?,?)""", (user["id"], doc_id, d, t, reason)).lastrowid
            db.execute("COMMIT")
        except ValueError as err:
            db.execute("ROLLBACK")
            flash(str(err), "error")
            return redirect(back)
        except Exception:
            db.execute("ROLLBACK")
            raise

        msg = f"Appointment #{appt_id} confirmed with {doc['name']} on {fmt_date(d)} at {fmt_time(t)}."
        notify(user["id"], msg)
        log_activity("book_appointment", f"#{appt_id}")
        flash(msg + " A confirmation was sent by email and SMS.", "success")
        return redirect(url_for("my_appointments"))

    dept_id = request.args.get("department", type=int) or 0
    doc_id = request.args.get("doctor", type=int) or 0
    date_str = request.args.get("date", "")
    doctors_in_dept = q("SELECT * FROM doctors WHERE department_id=? AND status='active' ORDER BY name",
                        (dept_id,)) if dept_id else []
    doctor = next((d for d in doctors_in_dept if d["id"] == doc_id), None)
    valid_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str)) and min_date <= date_str <= max_date
    slots = get_slots(doctor, date_str) if doctor and valid_date else []
    return render_template("book.html", depts=q("SELECT id,name FROM departments ORDER BY name"),
                           doctors=doctors_in_dept, doctor=doctor, dept_id=dept_id, doc_id=doc_id,
                           date_str=date_str if valid_date else "", valid_date=valid_date, slots=slots,
                           min_date=min_date, max_date=max_date, window=BOOKING_WINDOW_DAYS)


@app.route("/my-appointments")
@login_required("patient")
def my_appointments():
    rows = q("""SELECT a.*, d.name AS doctor, dp.name AS dept FROM appointments a
                JOIN doctors d ON d.id=a.doctor_id JOIN departments dp ON dp.id=d.department_id
                WHERE a.patient_id=? ORDER BY a.appointment_date DESC, a.appointment_time DESC""",
             (current_user()["id"],))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return render_template("my_appointments.html", rows=rows, now=now)


@app.route("/cancel/<int:appt_id>", methods=["POST"])
@login_required("patient")
def cancel(appt_id):
    u = current_user()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = ex("""UPDATE appointments SET status='Cancelled' WHERE id=? AND patient_id=? AND status='Booked'
                AND appointment_date || ' ' || appointment_time > ?""", (appt_id, u["id"], now))
    if cur.rowcount:
        log_activity("cancel_appointment", f"#{appt_id} by patient")
        notify(u["id"], f"Appointment #{appt_id} has been cancelled as requested.")
        flash(f"Appointment #{appt_id} cancelled.", "success")
    else:
        flash("That appointment cannot be cancelled.", "error")
    return redirect(url_for("my_appointments"))


@app.route("/notifications")
@login_required("patient")
def notifications():
    rows = q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (current_user()["id"],))
    return render_template("notifications.html", rows=rows)


# --------------------------------------------------------------------------- admin
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    one = lambda sql: q(sql, one=True)[0]
    stats = {
        "Patients": one("SELECT COUNT(*) FROM users WHERE role='patient'"),
        "Doctors": one("SELECT COUNT(*) FROM doctors WHERE status='active'"),
        "Departments": one("SELECT COUNT(*) FROM departments"),
        "Today's appointments": one("SELECT COUNT(*) FROM appointments WHERE appointment_date=date('now','localtime') AND status<>'Cancelled'"),
        "Upcoming (booked)": one("""SELECT COUNT(*) FROM appointments WHERE status='Booked'
                                    AND appointment_date||' '||appointment_time > strftime('%Y-%m-%d %H:%M','now','localtime')"""),
        "Completed": one("SELECT COUNT(*) FROM appointments WHERE status='Completed'"),
    }
    today = q("""SELECT a.*, u.name AS patient, d.name AS doctor FROM appointments a
                 JOIN users u ON u.id=a.patient_id JOIN doctors d ON d.id=a.doctor_id
                 WHERE a.appointment_date=date('now','localtime') ORDER BY a.appointment_time""")
    return render_template("admin/dashboard.html", stats=stats, today=today)


@app.route("/admin/appointments", methods=["GET", "POST"])
@login_required("admin")
def admin_appointments():
    if request.method == "POST":
        appt_id = request.form.get("id", type=int)
        status = request.form.get("status", "")
        if status in STATUSES:
            row = q("SELECT patient_id FROM appointments WHERE id=?", (appt_id,), one=True)
            if row:
                try:
                    ex("UPDATE appointments SET status=? WHERE id=?", (status, appt_id))
                except sqlite3.IntegrityError:
                    flash("Cannot restore this appointment: the slot has been booked by someone else.", "error")
                    return redirect(request.referrer or url_for("admin_appointments"))
                notify(row["patient_id"], f"Your appointment #{appt_id} status is now: {status}.")
                log_activity("update_status", f"#{appt_id} -> {status}")
                flash(f"Appointment #{appt_id} marked {status}.", "success")
        return redirect(request.referrer or url_for("admin_appointments"))

    f_status = request.args.get("status", "")
    f_date = request.args.get("date", "")
    f_q = request.args.get("q", "").strip()
    sql = """SELECT a.*, u.name AS patient, u.phone, d.name AS doctor, dp.name AS dept
             FROM appointments a JOIN users u ON u.id=a.patient_id JOIN doctors d ON d.id=a.doctor_id
             JOIN departments dp ON dp.id=d.department_id WHERE 1=1"""
    args = []
    if f_status in STATUSES:
        sql += " AND a.status=?"; args.append(f_status)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f_date):
        sql += " AND a.appointment_date=?"; args.append(f_date)
    if f_q:
        sql += " AND (u.name LIKE ? OR d.name LIKE ?)"; args += [f"%{f_q}%"] * 2
    rows = q(sql + " ORDER BY a.appointment_date DESC, a.appointment_time DESC LIMIT 200", args)
    return render_template("admin/appointments.html", rows=rows, statuses=STATUSES,
                           f_status=f_status, f_date=f_date, f_q=f_q)


@app.route("/admin/doctors", methods=["GET", "POST"])
@login_required("admin")
def admin_doctors():
    errors = []
    form = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle":
            ex("UPDATE doctors SET status = CASE status WHEN 'active' THEN 'inactive' ELSE 'active' END WHERE id=?",
               (request.form.get("id", type=int),))
            log_activity("toggle_doctor", f"#{request.form.get('id')}")
            flash("Doctor status updated.", "success")
            return redirect(url_for("admin_doctors"))

        f = request.form
        doc_id = f.get("id", type=int) or 0
        days = [d for d in DAYS if d in f.getlist("days")]
        data = {
            "name": f.get("name", "").strip(), "department_id": f.get("department_id", type=int) or 0,
            "qualification": f.get("qualification", "").strip(),
            "experience_years": max(0, f.get("experience_years", type=int) or 0),
            "phone": f.get("phone", "").strip(), "email": f.get("email", "").strip(),
            "work_days": ",".join(days), "start_time": f.get("start_time", "")[:5],
            "end_time": f.get("end_time", "")[:5], "slot_minutes": f.get("slot_minutes", type=int) or 0,
        }
        if len(data["name"]) < 3: errors.append("Doctor name is required.")
        if not data["department_id"]: errors.append("Select a department.")
        if not days: errors.append("Select at least one working day.")
        if not (re.fullmatch(r"\d{2}:\d{2}", data["start_time"]) and re.fullmatch(r"\d{2}:\d{2}", data["end_time"])
                and data["start_time"] < data["end_time"]):
            errors.append("End time must be after start time.")
        if not 5 <= data["slot_minutes"] <= 120: errors.append("Slot length must be 5-120 minutes.")
        if not errors:
            cols = list(data)
            if doc_id:
                ex(f"UPDATE doctors SET {', '.join(c + '=?' for c in cols)} WHERE id=?", [*data.values(), doc_id])
                log_activity("edit_doctor", f"#{doc_id} {data['name']}")
            else:
                ex(f"INSERT INTO doctors ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(data.values()))
                log_activity("add_doctor", data["name"])
            flash("Doctor saved.", "success")
            return redirect(url_for("admin_doctors"))
        form = {**data, "id": doc_id}

    if form is None:
        edit_id = request.args.get("edit", type=int)
        row = q("SELECT * FROM doctors WHERE id=?", (edit_id,), one=True) if edit_id else None
        form = dict(row) if row else {"id": 0, "name": "", "department_id": 0, "qualification": "", "experience_years": 0,
                                      "phone": "", "email": "", "work_days": "Mon,Tue,Wed,Thu,Fri",
                                      "start_time": "09:00", "end_time": "13:00", "slot_minutes": 20}
    docs = q("""SELECT d.*, dp.name AS dept FROM doctors d JOIN departments dp ON dp.id=d.department_id ORDER BY d.name""")
    return render_template("admin/doctors.html", errors=errors, f=form, fdays=form["work_days"].split(","),
                           docs=docs, depts=q("SELECT id,name FROM departments ORDER BY name"), days=DAYS)


@app.route("/admin/departments", methods=["GET", "POST"])
@login_required("admin")
def admin_departments():
    if request.method == "POST":
        if request.form.get("action") == "add":
            name = request.form.get("name", "").strip()
            desc = request.form.get("description", "").strip()[:250]
            if len(name) < 2:
                flash("Department name is required.", "error")
            else:
                try:
                    ex("INSERT INTO departments (name,description) VALUES (?,?)", (name, desc))
                    log_activity("add_department", name)
                    flash("Department added.", "success")
                except sqlite3.IntegrityError:
                    flash("A department with that name already exists.", "error")
        elif request.form.get("action") == "delete":
            dep_id = request.form.get("id", type=int)
            if q("SELECT 1 FROM doctors WHERE department_id=?", (dep_id,), one=True):
                flash("Cannot delete a department that still has doctors. Reassign or deactivate them first.", "error")
            else:
                ex("DELETE FROM departments WHERE id=?", (dep_id,))
                log_activity("delete_department", f"#{dep_id}")
                flash("Department deleted.", "success")
        return redirect(url_for("admin_departments"))
    rows = q("SELECT d.*, (SELECT COUNT(*) FROM doctors WHERE department_id=d.id) AS n FROM departments d ORDER BY name")
    return render_template("admin/departments.html", rows=rows)


@app.route("/admin/patients")
@login_required("admin")
def admin_patients():
    qtext = request.args.get("q", "").strip()
    sql = """SELECT u.id,u.name,u.email,u.phone,u.created_at,
             (SELECT COUNT(*) FROM appointments WHERE patient_id=u.id) AS total,
             (SELECT COUNT(*) FROM appointments WHERE patient_id=u.id AND status='Booked') AS upcoming
             FROM users u WHERE u.role='patient'"""
    args = []
    if qtext:
        sql += " AND (u.name LIKE ? OR u.email LIKE ? OR u.phone LIKE ?)"
        args = [f"%{qtext}%"] * 3
    return render_template("admin/patients.html", rows=q(sql + " ORDER BY u.created_at DESC LIMIT 300", args), qtext=qtext)


@app.route("/admin/reports")
@login_required("admin")
def admin_reports():
    d_from = request.args.get("from", "")
    d_to = request.args.get("to", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d_from):
        d_from = date.today().replace(day=1).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d_to):
        d_to = date.today().isoformat()
    rng = (d_from, d_to)

    if "csv" in request.args:
        rows = q("""SELECT a.id, a.appointment_date, a.appointment_time, u.name AS patient, d.name AS doctor,
                    dp.name AS department, a.status, a.reason
                    FROM appointments a JOIN users u ON u.id=a.patient_id JOIN doctors d ON d.id=a.doctor_id
                    JOIN departments dp ON dp.id=d.department_id
                    WHERE a.appointment_date BETWEEN ? AND ? ORDER BY a.appointment_date, a.appointment_time""", rng)
        log_activity("export_report", f"{d_from} to {d_to}")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ID", "Date", "Time", "Patient", "Doctor", "Department", "Status", "Reason"])
        for r in rows:  # neutralise spreadsheet formula injection
            w.writerow([("'" + str(v)) if isinstance(v, str) and v[:1] in "=+-@" else v for v in r])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=appointments_{d_from}_to_{d_to}.csv"})

    by_status = q("SELECT status, COUNT(*) n FROM appointments WHERE appointment_date BETWEEN ? AND ? GROUP BY status", rng)
    by_dept = q("""SELECT dp.name, COUNT(*) n FROM appointments a JOIN doctors d ON d.id=a.doctor_id
                   JOIN departments dp ON dp.id=d.department_id WHERE a.appointment_date BETWEEN ? AND ?
                   GROUP BY dp.id ORDER BY n DESC""", rng)
    by_doc = q("""SELECT d.name, COUNT(*) n, SUM(a.status='Completed') done, SUM(a.status='Cancelled') cancelled,
                  SUM(a.status='Missed') missed FROM appointments a JOIN doctors d ON d.id=a.doctor_id
                  WHERE a.appointment_date BETWEEN ? AND ? GROUP BY d.id ORDER BY n DESC""", rng)
    return render_template("admin/reports.html", d_from=d_from, d_to=d_to, by_status=by_status, by_dept=by_dept,
                           by_doc=by_doc, total=sum(r["n"] for r in by_status),
                           max_dept=max([1] + [r["n"] for r in by_dept]))


@app.route("/admin/logs")
@login_required("admin")
def admin_logs():
    rows = q("""SELECT l.*, u.name FROM activity_logs l LEFT JOIN users u ON u.id=l.user_id
                ORDER BY l.id DESC LIMIT 200""")
    return render_template("admin/logs.html", rows=rows)


@app.errorhandler(403)
def forbidden(_e):
    return "Access denied.", 403


init_db()

if __name__ == "__main__":
    app.run(debug=True)
