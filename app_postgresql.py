import os
from datetime import date, timedelta
import random
import secrets
import threading
import time
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:YOUR_PASSWORD@localhost:5432/lms_db"
)

DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "Lms#2026demo")  # password given to all demo users
BCRYPT_ROUNDS = 10          # cost factor of the bcrypt hash
MAX_ATTEMPTS = 5            # failed logins before the account is locked
LOCK_MINUTES = 20           # how long the account stays locked
SESSION_SECONDS = 2 * 60 * 60

app = Flask(__name__, static_folder="static")
SESSIONS = {}  # Demo-only in-memory sessions: token -> (user_id, expires_at)
# Failed-login counters live in server memory only (never in the database):
# email -> [failed_count, locked_until_timestamp]. They reset when the server restarts.
ATTEMPTS = {}
ATTEMPTS_LOCK = threading.Lock()
ALLOWED_ORIGINS = {"http://127.0.0.1:5500", "http://localhost:5500"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('student','teacher','admin')),
    password_hash VARCHAR(255)
);

-- for databases created before passwords were added
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
-- login attempts are no longer stored in the database
ALTER TABLE users DROP COLUMN IF EXISTS failed_attempts;
ALTER TABLE users DROP COLUMN IF EXISTS locked_until;

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE,
    title VARCHAR(150) NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS enrollments (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, course_id)
);

-- kinds of tasks: Quiz, Homework, Project, Midterm Exam, Final Exam ...
CREATE TABLE IF NOT EXISTS assignment_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
);

-- every task of a course: its type, form of control (current / midterm / final), max score, deadline
CREATE TABLE IF NOT EXISTS assignments (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title VARCHAR(100) NOT NULL,
    type_id INTEGER NOT NULL REFERENCES assignment_types(id),
    control_form VARCHAR(10) NOT NULL CHECK (control_form IN ('current','midterm','final')),
    max_score NUMERIC(7,2) NOT NULL CHECK (max_score > 0),
    due_date DATE NOT NULL,
    UNIQUE (course_id, title)
);
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS due_time TIME NOT NULL DEFAULT '23:59';

CREATE TABLE IF NOT EXISTS grades (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    score NUMERIC(7,2) NOT NULL,
    max_score NUMERIC(7,2) NOT NULL,
    CHECK (score >= 0 AND score <= max_score)
);

CREATE TABLE IF NOT EXISTS attendance (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    class_date DATE NOT NULL,
    present SMALLINT NOT NULL CHECK (present IN (0,1))
);

CREATE TABLE IF NOT EXISTS project_groups (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name VARCHAR(60) NOT NULL,
    max_size INTEGER NOT NULL CHECK (max_size BETWEEN 2 AND 6),
    created_by INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES project_groups(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
"""


ASSIGNMENT_TYPES = ["Quiz", "Homework", "Project", "Midterm Exam", "Final Exam"]

# title, type, control form, max score, deadline, time, already graded?, spread (days)
# Graded tasks are finished. The others show up in the Dashboard timeline (the spread moves the
# deadline by 0..spread-1 days depending on the course, so courses do not all end on the same day).
ASSIGNMENT_PLAN = [
    ("Quiz 1",       "Quiz",         "current", 10, "2026-09-10", "23:59", True,  1),
    ("Homework 1",   "Homework",     "current", 20, "2026-09-24", "23:59", True,  1),
    ("Midterm Exam", "Midterm Exam", "midterm", 30, "2026-10-15", "23:00", True,  1),
    ("Project",      "Project",      "current", 20, "2026-10-10", "23:59", False, 3),
    ("Homework 2",   "Homework",     "current", 20, "2026-10-25", "23:50", False, 5),
    ("Final Exam",   "Final Exam",   "final",   40, "2026-12-15", "23:00", False, 4),
]


def plan_for_course(course_id):
    rows = []
    for title, type_name, form, max_score, due, due_time, graded, spread in ASSIGNMENT_PLAN:
        due_day = date.fromisoformat(due) + timedelta(days=course_id % spread)
        rows.append((title, type_name, form, max_score, due_day.isoformat(), due_time, graded))
    return rows


def insert_assignment(cur, course_id, row):
    """Adds one task to a course (skips it if the course already has a task with this title)."""
    title, type_name, form, max_score, due_day, due_time, _graded = row
    cur.execute(
        """INSERT INTO assignments(course_id,title,type_id,control_form,max_score,due_date,due_time)
           SELECT %s, %s, id, %s, %s, %s::date, %s::time FROM assignment_types WHERE name = %s
           ON CONFLICT (course_id, title) DO NOTHING
           RETURNING id""",
        (course_id, title, form, max_score, due_day, due_time, type_name)
    )
    found = cur.fetchone()
    return found[0] if found else None


# One teacher per course (course 1 -> first teacher, course 2 -> second, ...).
TEACHERS = [
    (1,   "teacher@astanait.edu.kz",        "Diana Kazbekova"),
    (102, "a.sarsenov@astanait.edu.kz",     "Aidar Sarsenov"),
    (103, "m.omarova@astanait.edu.kz",      "Madina Omarova"),
    (104, "e.tulegenov@astanait.edu.kz",    "Erlan Tulegenov"),
    (105, "a.nurpeisova@astanait.edu.kz",   "Aigerim Nurpeisova"),
    (106, "b.zhaksylykov@astanait.edu.kz",  "Bauyrzhan Zhaksylykov"),
    (107, "s.abenova@astanait.edu.kz",      "Saltanat Abenova"),
    (108, "d.mukhamedov@astanait.edu.kz",   "Daniyar Mukhamedov"),
    (109, "g.kenzhebayeva@astanait.edu.kz", "Gulnara Kenzhebayeva"),
    (110, "t.alimov@astanait.edu.kz",       "Timur Alimov"),
]


def hash_password(password):
    """bcrypt with a random salt: the same password gives a different hash every time."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def check_password(password, hashed):
    raw = password.encode()
    if len(raw) > 72:  # bcrypt limit
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode())
    except ValueError:
        return False


# Used when the e-mail does not exist, so the answer takes the same time as for a real account.
DUMMY_HASH = hash_password("not-a-real-password")


def db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(_error):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query_one(sql, args=()):
    with db().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def rows(sql, args=()):
    with db().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(row) for row in cur.fetchall()]


def execute(sql, args=(), returning=False):
    conn = db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        result = dict(cur.fetchone()) if returning and cur.description else None
    conn.commit()
    return result


def migrate_old_database(cur):
    """Brings a database made by an older version of this file up to date (does nothing for a new one)."""
    # 1) grades used a free-text column "item" -> now they point to a row of the assignments table
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'grades' AND column_name = 'item'""")
    if cur.fetchone():
        names = "CASE g.item WHEN 'Assignment' THEN 'Homework' WHEN 'Midterm' THEN 'Midterm Exam' ELSE g.item END"
        cur.execute("ALTER TABLE grades ADD COLUMN IF NOT EXISTS assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE")
        cur.execute(f"INSERT INTO assignment_types(name) SELECT DISTINCT {names} FROM grades g ON CONFLICT DO NOTHING")
        cur.execute(f"""
            INSERT INTO assignments(course_id,title,type_id,control_form,max_score,due_date)
            SELECT g.course_id, g.item, t.id,
                   CASE g.item WHEN 'Midterm' THEN 'midterm' ELSE 'current' END,
                   MAX(g.max_score), DATE '2026-09-30'
            FROM grades g JOIN assignment_types t ON t.name = {names}
            GROUP BY g.course_id, g.item, t.id
            ON CONFLICT DO NOTHING""")
        cur.execute("""UPDATE grades SET assignment_id = a.id FROM assignments a
                       WHERE a.course_id = grades.course_id AND a.title = grades.item""")
        cur.execute("ALTER TABLE grades ALTER COLUMN assignment_id SET NOT NULL")
        cur.execute("ALTER TABLE grades DROP COLUMN item")

    # 2) all courses had the same teacher -> give every course its own teacher
    cur.execute("SELECT COUNT(*) FROM users WHERE role = 'teacher'")
    teacher_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM courses")
    course_count = cur.fetchone()[0]
    if course_count and teacher_count < 2:
        for tid, mail, name in TEACHERS:
            cur.execute(
                """INSERT INTO users(id,email,full_name,role,password_hash) VALUES (%s,%s,%s,'teacher',%s)
                   ON CONFLICT DO NOTHING""",
                (tid, mail, name, hash_password(DEMO_PASSWORD)))
        for i, (tid, _mail, _name) in enumerate(TEACHERS, 1):
            cur.execute("UPDATE courses SET teacher_id = %s WHERE id = %s", (tid, i))

    # 3) make sure every course has upcoming / not yet graded tasks for the Dashboard timeline
    cur.execute("SELECT id FROM courses")
    for (course_id,) in cur.fetchall():
        for row in plan_for_course(course_id):
            if not row[6]:
                insert_assignment(cur, course_id, row)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            for type_name in ASSIGNMENT_TYPES:
                cur.execute("INSERT INTO assignment_types(name) VALUES (%s) ON CONFLICT DO NOTHING", (type_name,))
            migrate_old_database(cur)
            cur.execute("SELECT COUNT(*) FROM users")
            user_count = cur.fetchone()[0]

            # Create demo data only on the first run, so existing data is not overwritten.
            if user_count == 0:
                random.seed(1)
                for tid, mail, name in TEACHERS:
                    cur.execute(
                        "INSERT INTO users(id,email,full_name,role) VALUES (%s,%s,%s,'teacher')",
                        (tid, mail, name)
                    )
                titles = [
                    "ICT", "Databases", "Calculus", "Physics", "English",
                    "Algorithms", "Web Development", "Statistics",
                    "Discrete Math", "Networks"
                ]
                course_assignments = {}  # course id -> [(assignment id, max score, graded?)]
                for i, title in enumerate(titles, 1):
                    cur.execute(
                        "INSERT INTO courses(id,code,title,teacher_id) VALUES (%s,%s,%s,%s)",
                        (i, f"CSE{100+i}", title, TEACHERS[i - 1][0])
                    )
                    course_assignments[i] = []
                    for row in plan_for_course(i):
                        course_assignments[i].append((insert_assignment(cur, i, row), row[3], row[6]))
                for uid in range(2, 102):
                    cur.execute(
                        "INSERT INTO users(id,email,full_name,role) VALUES (%s,%s,%s,%s)",
                        (uid, f"26{uid:04d}@astanait.edu.kz", f"Student {uid-1}", "student")
                    )
                    for course_id in random.sample(range(1, 11), 5):
                        cur.execute(
                            "INSERT INTO enrollments(user_id,course_id) VALUES (%s,%s)",
                            (uid, course_id)
                        )
                        for k in range(8):
                            cur.execute(
                                """INSERT INTO attendance(user_id,course_id,class_date,present)
                                   VALUES (%s,%s,%s,%s)""",
                                (uid, course_id, f"2026-09-{k*3+1:02d}", int(random.random() < 0.88))
                            )
                        for assignment_id, max_score, graded in course_assignments[course_id]:
                            if not graded:
                                continue  # e.g. the final exam has not taken place yet
                            score = round(random.uniform(0.5, 1) * max_score, 1)
                            cur.execute(
                                """INSERT INTO grades(user_id,course_id,assignment_id,score,max_score)
                                   VALUES (%s,%s,%s,%s,%s)""",
                                (uid, course_id, assignment_id, score, max_score)
                            )

                # Keep the manually assigned demo IDs available for future inserts.
                cur.execute("""
                    SELECT setval(pg_get_serial_sequence('grades','id'),
                                  COALESCE((SELECT MAX(id) FROM grades), 1))
                """)
                cur.execute("""
                    SELECT setval(pg_get_serial_sequence('attendance','id'),
                                  COALESCE((SELECT MAX(id) FROM attendance), 1))
                """)

            # Give every user without a password hash the demo password (each hash has its own salt),
            # then make the column mandatory.
            cur.execute("SELECT id FROM users WHERE password_hash IS NULL")
            for (uid,) in cur.fetchall():
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (hash_password(DEMO_PASSWORD), uid))
            cur.execute("ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.after_request
def cors(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


@app.errorhandler(Exception)
def handle_error(err):
    """Always answer with JSON so the interface can show a readable message."""
    if isinstance(err, HTTPException):
        return jsonify(error=err.description), err.code
    app.logger.exception(err)
    if isinstance(err, psycopg2.OperationalError):
        return jsonify(error="Database is unavailable - check DATABASE_URL"), 503
    return jsonify(error="Internal server error"), 500


@app.route("/api/<path:_path>", methods=["OPTIONS"])
def preflight(_path):
    return "", 204


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/icons/<path:filename>")
def icons(filename):
    return send_from_directory("static/icons", filename)


@app.get("/login-bg.jpg")
def login_bg():
    return send_from_directory("static", "login-bg.jpg")


def current_user():
    token = request.headers.get("Authorization", "")
    session = SESSIONS.get(token)
    if not session:
        return None
    uid, expires_at = session
    if expires_at < time.time():
        SESSIONS.pop(token, None)
        return None
    return query_one("SELECT * FROM users WHERE id = %s", (uid,))


def is_locked(email):
    with ATTEMPTS_LOCK:
        entry = ATTEMPTS.get(email)
        if not entry:
            return False
        if entry[1] and entry[1] <= time.time():  # lock expired -> forget this e-mail
            ATTEMPTS.pop(email, None)
            return False
        return entry[1] > time.time()


def register_failure(email):
    with ATTEMPTS_LOCK:
        now = time.time()
        for key in [k for k, v in ATTEMPTS.items() if v[1] and v[1] <= now]:
            ATTEMPTS.pop(key, None)  # clean up expired locks
        if email not in ATTEMPTS and len(ATTEMPTS) >= 10000:
            return  # protection against filling the memory with random e-mails
        entry = ATTEMPTS.setdefault(email, [0, 0])
        entry[0] += 1
        if entry[0] >= MAX_ATTEMPTS:  # 5th failure -> lock and start counting again
            entry[0] = 0
            entry[1] = now + LOCK_MINUTES * 60


def clear_failures(email):
    with ATTEMPTS_LOCK:
        ATTEMPTS.pop(email, None)


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if is_locked(email):
        return jsonify(error=f"Account is locked for {LOCK_MINUTES} minutes after too many failed attempts"), 429

    user = query_one("SELECT * FROM users WHERE email = %s", (email,))

    # Always run one bcrypt check, so the reply does not show whether the e-mail exists.
    ok = check_password(password, user["password_hash"] if user else DUMMY_HASH) and user is not None
    if not ok:
        register_failure(email)  # counted in memory, nothing is written to the database
        return jsonify(error="Wrong e-mail or password"), 401

    clear_failures(email)
    token = secrets.token_hex(16)
    SESSIONS[token] = (user["id"], time.time() + SESSION_SECONDS)
    return jsonify(token=token, name=user["full_name"], email=user["email"], role=user["role"])


@app.post("/api/logout")
def logout():
    SESSIONS.pop(request.headers.get("Authorization", ""), None)
    return jsonify(ok=True)


def guard():
    user = current_user()
    return user, (None if user else (jsonify(error="Not signed in"), 401))


@app.get("/api/courses")
def courses():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT c.id, c.code, c.title, t.full_name AS teacher
        FROM enrollments e
        JOIN courses c ON c.id = e.course_id
        JOIN users t ON t.id = c.teacher_id
        WHERE e.user_id = %s
        ORDER BY c.title
    """, (user["id"],)))


@app.get("/api/grades")
def grades():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT c.title AS course, SUM(g.score) AS score, SUM(g.max_score) AS max_score
        FROM grades g JOIN courses c ON c.id = g.course_id
        WHERE g.user_id = %s
        GROUP BY c.id, c.title
        ORDER BY c.title
    """, (user["id"],)))


@app.get("/api/assignments")
def assignments():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT a.id, c.title AS course, teacher.full_name AS teacher, a.title, t.name AS type,
               a.control_form, a.max_score, a.due_date::text AS due_date,
               to_char(a.due_time, 'HH24:MI') AS due_time,
               (a.due_date + a.due_time) < localtimestamp AS overdue,
               g.score
        FROM assignments a
        JOIN courses c ON c.id = a.course_id
        JOIN users teacher ON teacher.id = c.teacher_id
        JOIN assignment_types t ON t.id = a.type_id
        JOIN enrollments e ON e.course_id = a.course_id AND e.user_id = %s
        LEFT JOIN grades g ON g.assignment_id = a.id AND g.user_id = %s
        ORDER BY a.due_date, a.due_time, c.title
    """, (user["id"], user["id"])))


@app.get("/api/attendance")
def attendance():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT c.title AS course, COUNT(*) AS total, SUM(a.present) AS present
        FROM attendance a JOIN courses c ON c.id = a.course_id
        WHERE a.user_id = %s
        GROUP BY c.id, c.title
        ORDER BY c.title
    """, (user["id"],)))


@app.get("/api/dashboard")
def dashboard():
    user, error = guard()
    if error:
        return error
    uid = user["id"]
    courses_count = query_one(
        "SELECT COUNT(*) AS value FROM enrollments WHERE user_id = %s", (uid,)
    )["value"]
    grade = query_one(
        "SELECT 100.0 * SUM(score) / NULLIF(SUM(max_score), 0) AS value FROM grades WHERE user_id = %s",
        (uid,)
    )["value"]
    attendance_pct = query_one(
        "SELECT 100.0 * SUM(present) / NULLIF(COUNT(*), 0) AS value FROM attendance WHERE user_id = %s",
        (uid,)
    )["value"]
    group_count = query_one(
        "SELECT COUNT(*) AS value FROM group_members WHERE user_id = %s", (uid,)
    )["value"]
    return jsonify(
        courses=courses_count or 0,
        avg_grade=round(float(grade or 0), 1),
        attendance=round(float(attendance_pct or 0), 1),
        groups=group_count or 0
    )


@app.get("/api/groups")
def groups():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT p.id, p.name, p.max_size, c.title AS course,
               (SELECT COUNT(*) FROM group_members m WHERE m.group_id = p.id) AS members
        FROM project_groups p
        JOIN courses c ON c.id = p.course_id
        JOIN group_members me ON me.group_id = p.id AND me.user_id = %s
        ORDER BY p.id DESC
    """, (user["id"],)))


@app.post("/api/groups")
def create_group():
    user, error = guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    try:
        course_id = int(data.get("course_id"))
        max_size = int(data.get("max_size"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid input"), 400

    if not 1 <= len(name) <= 60 or not 2 <= max_size <= 6:
        return jsonify(error="Name 1-60 chars, size 2-6"), 400

    enrolled = query_one(
        "SELECT 1 AS found FROM enrollments WHERE user_id = %s AND course_id = %s",
        (user["id"], course_id)
    )
    if not enrolled:
        return jsonify(error="You are not enrolled in this course"), 403

    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO project_groups(course_id,name,max_size,created_by)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (course_id, name, max_size, user["id"])
            )
            group_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO group_members(group_id,user_id) VALUES (%s,%s)",
                (group_id, user["id"])
            )
        conn.commit()  # both inserts succeed together or not at all
    except Exception:
        conn.rollback()
        raise
    return jsonify(id=group_id), 201


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    user, error = guard()
    if error:
        return error
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM project_groups WHERE id = %s AND created_by = %s",
            (group_id, user["id"])
        )
        deleted = cur.rowcount
    conn.commit()
    if deleted:
        return jsonify(ok=True)
    return jsonify(error="Group not found or you are not its creator"), 403


if __name__ == "__main__":
    init_db()
    print("Open http://127.0.0.1:5000")
    print(f"Demo login: 260002@astanait.edu.kz / {DEMO_PASSWORD}")
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)