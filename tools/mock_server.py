"""Test stub for the LMS API (no PostgreSQL available in this sandbox).
Serves the REAL static files of one variant plus fake JSON. Used only to measure/verify the front-end."""
import sys, os
from flask import Flask, jsonify, request, send_from_directory
root, port = sys.argv[1], int(sys.argv[2])
app = Flask(__name__)
S = os.path.join(root, "static")
groups = [{"id": 1, "name": "Team Alpha", "course": "Databases", "members": 3, "max_size": 4}]
courses = [{"id": i + 1, "code": c, "title": t, "teacher": "Test Teacher"} for i, (c, t) in
           enumerate([("MATH101", "Calculus"), ("CS201", "Databases"), ("ENG101", "English"), ("ICT101", "ICT"), ("PHY101", "Physics")])]
@app.get("/")
def index(): return send_from_directory(S, "index.html")
@app.get("/icons/<path:f>")
def icons(f): return send_from_directory(os.path.join(S, "icons"), f, max_age=86400)
@app.get("/media/<path:f>")
def media(f): return send_from_directory(os.path.join(S, "media"), f, max_age=86400)
@app.post("/api/login")
def login(): return jsonify(token="t", name="Test Student", email="260002@astanait.edu.kz")
@app.post("/api/logout")
def logout(): return jsonify(ok=1)
@app.get("/api/courses")
def api_courses(): return jsonify(courses)
@app.get("/api/dashboard")
def dash(): return jsonify(courses=5, avg_grade=82.3, groups=len(groups))
@app.get("/api/assignments")
def asg(): return jsonify([{"title": "HW 1", "type": "Assignment", "course": "ICT", "teacher": "Test Teacher", "due_date": "2026-11-02",
                            "due_time": "23:59", "control_form": "current", "overdue": False, "score": None}])
@app.get("/api/attendance")
def att(): return jsonify([{"course": "ICT", "present": 8, "total": 10}])
@app.get("/api/grades")
def grd(): return jsonify([{"course": "ICT", "score": 88, "max_score": 100}])
@app.route("/api/groups", methods=["GET", "POST"])
def grp():
    if request.method == "POST":
        d = request.get_json(); groups.append({"id": len(groups) + 10, "name": d["name"], "course": "ICT", "members": 1, "max_size": int(d["max_size"])})
        return jsonify(ok=1), 201
    return jsonify(groups)
@app.delete("/api/groups/<int:i>")
def delg(i):
    groups[:] = [g for g in groups if g["id"] != i]; return jsonify(ok=1)
app.run(port=port)
