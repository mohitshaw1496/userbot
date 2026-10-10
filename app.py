import os
import subprocess
import sys

from web_server import app

processes = []


def start_process(script):
    process = subprocess.Popen([sys.executable, script])
    processes.append(process)


if __name__ == "__main__":
    start_process("manager_bot.py")

    if os.environ.get("TG_STRING_SESSION"):
        start_process("user_client.py")
    else:
        print("TG_STRING_SESSION is missing; user client not started.")

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
