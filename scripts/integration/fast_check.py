import sys
import sqlite3
import os
import time
from datetime import datetime, timedelta

def main():
    notify_only = "--notify-only" in sys.argv
    # Remove flag from args to not mess up indexing if present
    args = [a for a in sys.argv if a != "--notify-only"]

    if len(args) < 4:
        return

    db_path = args[1]
    project_path = args[2]
    agent_name = args[3]

    if not agent_name:
        return

    if not os.path.exists(db_path):
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Get project ID
        cursor.execute("SELECT id FROM projects WHERE human_key = ?", (project_path,))
        row = cursor.fetchone()
        if not row: return
        project_id = row[0]

        # Get my agent ID
        cursor.execute("SELECT id FROM agents WHERE project_id = ? AND name = ?", (project_id, agent_name))
        row = cursor.fetchone()
        my_agent_id = row[0] if row else -1

        # Check for pending ACKs (messages requiring attention)
        cursor.execute("""
            SELECT m.subject, a.name
            FROM messages m
            JOIN message_recipients mr ON m.id = mr.message_id
            JOIN agents a ON m.sender_id = a.id
            WHERE m.project_id = ?
            AND mr.agent_id = ?
            AND mr.ack_ts IS NULL
            AND m.ack_required = 1
        """, (project_id, my_agent_id))

        messages = cursor.fetchall()
        if messages:
             print(f"\n📩 You have {len(messages)} pending message(s):")
             for subject, sender in messages:
                 print(f"  - From {sender}: {subject}")
             print("")

        # Check for active conflicting reservations (exclusive from others)
        now = datetime.utcnow()
        now_str = str(now)

        cursor.execute("""
            SELECT path_pattern, expires_ts
            FROM file_reservations
            WHERE project_id = ?
            AND released_ts IS NULL
            AND exclusive = 1
            AND agent_id != ?
            AND expires_ts > ?
        """, (project_id, my_agent_id, now_str))

        alerts = []
        for path, expires_str in cursor.fetchall():
             alerts.append(f"LOCKED by other agent: {path}")

        if alerts:
            if notify_only:
                print("\n⚠️  ACTIVE RESERVATIONS (INFO):")
                for a in alerts:
                    print(f"  - {a}")
            else:
                print("\n⛔ OPERATION BLOCKED BY ACTIVE RESERVATIONS:")
                for a in alerts:
                    print(f"  - {a}")
                print("Wait for the reservation to expire or be released.")
                sys.exit(1)

    except Exception:
        pass

if __name__ == "__main__":
    main()
