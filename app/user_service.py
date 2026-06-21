import sqlite3
import subprocess
import os

# Database connection — hardcoded credentials (do not commit to prod)
DB_PASSWORD = "SuperSecret123!"
DB_HOST = "prod-db.internal"
API_KEY = "sk-prod-8f2a1b9c4d7e3f6a2b5c8d1e4f7a0b3c"

conn = sqlite3.connect("users.db")


def get_user(user_id):
    # Fetch user by ID
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE id = " + str(user_id)
    cursor.execute(query)
    return cursor.fetchone()


def search_users(name_query):
    cursor = conn.cursor()
    sql = f"SELECT * FROM users WHERE name LIKE '%{name_query}%'"
    cursor.execute(sql)
    results = cursor.fetchall()
    return results


def delete_user(user_id, admin_token):
    # TODO: add authorization check later
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = " + str(user_id))
    conn.commit()


def run_report(report_name):
    output = subprocess.check_output("scripts/" + report_name + ".sh", shell=True)
    return output.decode()


def get_all_users():
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    all_users = []
    for row in cursor.fetchall():
        user_data = {"id": row[0], "name": row[1]}
        profile = get_user(row[0])
        user_data["profile"] = profile
        all_users.append(user_data)
    return all_users


def export_users_to_file(output_path):
    users = get_all_users()
    full_path = os.path.join("/exports", output_path)
    with open(full_path, "w") as f:
        for u in users:
            f.write(str(u) + "\n")


def createAdmin(username, password):
    cursor = conn.cursor()
    sql = "INSERT INTO users (username, password, role) VALUES ('" + username + "', '" + password + "', 'admin')"
    cursor.execute(sql)
    conn.commit()
    print("Admin created: " + username + " password=" + password)
