
import os
import io
import csv
import secrets
from functools import wraps

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    session,
    flash,
    request,
    Response,
)
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(
    __name__,
    template_folder="static/templates",
    static_folder="static",
)

app.config["SECRET_KEY"] = (
    os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

oauth = OAuth(app)

client_id = os.getenv("GOOGLE_CLIENT_ID")
client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

if client_id and client_secret:
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


# Temporary memory storage.
# Data resets when Flask restarts.
students = []
halls = []
allocations = []


# Login protection
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("home"))
        return view(*args, **kwargs)

    return wrapped


# Home page
@app.route("/")
def home():
    if session.get("user"):
        return redirect(url_for("dashboard"))

    return render_template("index.html")


# Google login
@app.route("/login/google")
def google_login():
    if not client_id or not client_secret:
        flash(
            "Google login credentials are missing. Check your .env file."
        )
        return redirect(url_for("home"))

    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


# Google authentication callback
@app.route("/auth/callback")
def auth_callback():
    if not client_id or not client_secret:
        flash("Google login is not configured.")
        return redirect(url_for("home"))

    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo")

        if not user_info:
            user_info = oauth.google.userinfo()

        session["user"] = {
            "name": user_info.get("name", "User"),
            "email": user_info.get("email", ""),
            "picture": user_info.get("picture", ""),
        }

        return redirect(url_for("dashboard"))

    except Exception:
        app.logger.exception("Google sign-in failed")
        flash(
            "Google sign-in failed. Check OAuth settings and try again."
        )
        return redirect(url_for("home"))


# Dashboard
@app.route("/dashboard")
@login_required
def dashboard():
    capacity = sum(
        hall["rows"] * hall["columns"]
        for hall in halls
    )

    stats = {
        "students": len(students),
        "halls": len(halls),
        "available": max(capacity - len(allocations), 0),
        "allocated": len(allocations),
    }

    return render_template(
        "dashboard.html",
        user=session["user"],
        stats=stats,
    )


# Student management and CSV upload
@app.route("/students", methods=["GET", "POST"])
@login_required
def students_page():
    message = ""

    if request.method == "POST":
        uploaded = request.files.get("file")

        if not uploaded or not uploaded.filename:
            message = "Please choose a CSV file."

        elif not uploaded.filename.lower().endswith(".csv"):
            message = "Please upload a CSV file."

        else:
            try:
                content = uploaded.stream.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(content))

                if not reader.fieldnames:
                    raise ValueError("The CSV file is empty.")

                headers = {
                    header.strip().lower(): header
                    for header in reader.fieldnames
                    if header
                }

                reg_key = (
                    headers.get("register no")
                    or headers.get("register number")
                    or headers.get("register_no")
                )

                name_key = headers.get("name")
                subject_key = headers.get("subject")

                if not all([reg_key, name_key, subject_key]):
                    raise ValueError(
                        "CSV must contain Register No, Name, "
                        "and Subject columns."
                    )

                seen = {
                    student["Register No"].casefold()
                    for student in students
                }

                imported = []

                for row in reader:
                    reg = (row.get(reg_key) or "").strip()
                    name = (row.get(name_key) or "").strip()
                    subject = (row.get(subject_key) or "").strip()

                    if not reg or not name or not subject:
                        continue

                    if reg.casefold() in seen:
                        continue

                    seen.add(reg.casefold())

                    imported.append({
                        "Register No": reg,
                        "Name": name,
                        "Subject": subject,
                    })

                students.extend(imported)

                # Existing seating must be regenerated after importing.
                allocations.clear()

                message = (
                    f"Imported {len(imported)} new student record(s)."
                )

            except (
                UnicodeDecodeError,
                csv.Error,
                ValueError,
            ) as error:
                message = f"CSV import failed: {error}"

    query = request.args.get("q", "").strip().casefold()

    filtered = [
        student
        for student in students
        if query in student["Register No"].casefold()
        or query in student["Name"].casefold()
        or query in student["Subject"].casefold()
    ]

    return render_template(
        "students.html",
        user=session["user"],
        students=filtered,
        total_students=len(students),
        query=query,
        message=message,
    )


# Seating arrangement, hall management and seat search
@app.route("/seating", methods=["GET", "POST"])
@login_required
def seating_page():
    message = ""

    if request.method == "POST":
        action = request.form.get("action", "add_hall")

        # Add an examination hall
        if action == "add_hall":
            name = request.form.get("hall_name", "").strip()

            try:
                rows = int(request.form.get("rows", "0"))
                columns = int(request.form.get("columns", "0"))

                if not name:
                    raise ValueError("Enter a hall name.")

                if not (1 <= rows <= 100 and 1 <= columns <= 100):
                    raise ValueError(
                        "Rows and columns must be from 1 to 100."
                    )

                if any(
                    hall["name"].casefold() == name.casefold()
                    for hall in halls
                ):
                    raise ValueError(
                        "That hall name already exists."
                    )

                halls.append({
                    "name": name,
                    "rows": rows,
                    "columns": columns,
                })

                # Clear old allocations when hall setup changes.
                allocations.clear()

                message = f"{name} added successfully."

            except ValueError as error:
                message = str(error)

        # Generate seating allocations
        elif action == "generate":
            capacity = sum(
                hall["rows"] * hall["columns"]
                for hall in halls
            )

            if not students:
                message = (
                    "Upload students before generating seating."
                )

            elif not halls:
                message = "Add an examination hall first."

            elif len(students) > capacity:
                message = (
                    f"Not enough seats: {len(students)} students, "
                    f"{capacity} seats."
                )

            else:
                new_allocations = []
                index = 0

                for hall in halls:
                    for row in range(1, hall["rows"] + 1):
                        for col in range(1, hall["columns"] + 1):

                            if index >= len(students):
                                break

                            student = students[index]

                            new_allocations.append({
                                **student,
                                "Hall": hall["name"],
                                "Row": row,
                                "Column": col,
                                "Seat": f"R{row}-C{col}",
                            })

                            index += 1

                        if index >= len(students):
                            break

                    if index >= len(students):
                        break

                allocations[:] = new_allocations

                message = (
                    f"Allocated {len(allocations)} students "
                    "successfully."
                )

        else:
            message = "Invalid action."

    # Calculate seating capacity
    capacity = sum(
        hall["rows"] * hall["columns"]
        for hall in halls
    )

    # Search by name or register number.
    # Search ignores letter case and supports partial matches.
    search_query = request.args.get("q", "").strip().casefold()
    search_result = None

    if search_query:
        search_result = next(
            (
                item
                for item in allocations
                if search_query in item["Register No"].casefold()
                or search_query in item["Name"].casefold()
            ),
            None,
        )

    return render_template(
        "seating.html",
        user=session["user"],
        halls=halls,
        allocations=allocations,
        message=message,
        total_students=len(students),
        total_capacity=capacity,
        available_seats=max(capacity - len(allocations), 0),
        search_query=search_query,
        search_result=search_result,
    )


# Export seating arrangement to CSV
@app.route("/export")
@login_required
def export_csv():
    if not allocations:
        flash("Generate seating before exporting.")
        return redirect(url_for("seating_page"))

    output = io.StringIO()

    fields = [
        "Register No",
        "Name",
        "Subject",
        "Hall",
        "Row",
        "Column",
        "Seat",
    ]

    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(allocations)

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                "attachment; filename=exam_seating_arrangement.csv"
            )
        },
    )


# Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# Run the application
if __name__ == "__main__":
    app.run(debug=True)
