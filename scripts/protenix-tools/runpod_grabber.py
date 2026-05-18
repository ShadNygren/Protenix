#!/usr/bin/env python3
"""Poll RunPod API until an RTX 5090 pod is acquired, then auto-launch training.

Designed to run unattended on your laptop. Polls the RunPod GraphQL API at
a configurable interval (default 1s) until a pod is successfully deployed.
Once acquired, waits for SSH availability, delivers R2 credentials, and
launches the autonomous runner script on the pod.

Credential sources (checked in order):
  1. --env-file (default: project .env)
  2. CLOUDFLARE_R2_* environment variables
  3. ~/.aws/credentials [cloudflare-r2] profile (access key + secret only)

Usage:
    # Basic: grab a pod and launch default training
    python3 runpod_grabber.py

    # With a training plan (chain of runs)
    python3 runpod_grabber.py --plan training_plan.json

    # Just grab the pod, don't auto-launch
    python3 runpod_grabber.py --no-launch

    # Custom poll interval and cloud type
    python3 runpod_grabber.py --interval 2 --cloud-type ALL
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import sys
import time
import urllib.request


def get_api_key() -> str:
    config_path = os.path.expanduser("~/.runpod/config.toml")
    if not os.path.exists(config_path):
        raise RuntimeError(f"RunPod config not found at {config_path}")
    with open(config_path) as f:
        for line in f:
            if line.strip().startswith("apikey"):
                return line.split('"')[1]
    raise RuntimeError("No apikey found in ~/.runpod/config.toml")


def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE .env file, stripping quotes and comments."""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            env[key.strip()] = val
    return env


def _find_env_file() -> str | None:
    """Auto-discover .env file in known project locations."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "..", "..", ".env"),
        os.path.join(
            script_dir, "..", "..", "..",
            "UHRF1_inhibition_by_STELLA_for_cancer_therapy", ".env",
        ),
        os.path.expanduser(
            "~/github/ShadNygren/UHRF1_inhibition_by_STELLA_for_cancer_therapy/.env"
        ),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def get_r2_creds(env_file: str | None) -> dict[str, str]:
    """Collect R2 credentials from env file, env vars, or AWS credentials."""
    keys = [
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_ENDPOINT",
        "CLOUDFLARE_ACCOUNT_ID",
    ]

    creds: dict[str, str] = {k: "" for k in keys}

    # Source 1: env file (explicit or auto-discovered)
    if env_file is None:
        env_file = _find_env_file()
    if env_file:
        file_env = load_env_file(env_file)
        for k in keys:
            if file_env.get(k):
                creds[k] = file_env[k]

    # Source 2: environment variables (override file)
    for k in keys:
        val = os.environ.get(k, "")
        if val:
            creds[k] = val

    # Source 3: AWS credentials file for access key + secret
    if not creds["CLOUDFLARE_R2_ACCESS_KEY_ID"]:
        config = configparser.ConfigParser()
        config.read(os.path.expanduser("~/.aws/credentials"))
        if "cloudflare-r2" in config:
            profile = config["cloudflare-r2"]
            creds["CLOUDFLARE_R2_ACCESS_KEY_ID"] = profile.get(
                "aws_access_key_id", ""
            )
            creds["CLOUDFLARE_R2_SECRET_ACCESS_KEY"] = profile.get(
                "aws_secret_access_key", ""
            )

    # Construct endpoint from account ID if not set
    if not creds["CLOUDFLARE_R2_ENDPOINT"] and creds["CLOUDFLARE_ACCOUNT_ID"]:
        creds["CLOUDFLARE_R2_ENDPOINT"] = (
            f"https://{creds['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        )

    return creds


def graphql(api_key: str, query: str) -> dict:
    url = f"https://api.runpod.io/graphql?api_key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": query}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "runpod-grabber/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def poll_for_pod(
    api_key: str,
    template_id: str,
    gpu_type: str,
    cloud_type: str,
    volume_gb: int,
    name: str,
    interval: float,
) -> dict:
    """Poll RunPod API until a pod is successfully deployed."""
    query = f"""mutation {{
        podFindAndDeployOnDemand(input: {{
            name: "{name}",
            templateId: "{template_id}",
            gpuTypeId: "{gpu_type}",
            cloudType: {cloud_type},
            gpuCount: 1,
            startJupyter: true,
            startSsh: true,
            volumeInGb: {volume_gb}
        }}) {{
            id name desiredStatus costPerHr
            machine {{ gpuDisplayName location }}
        }}
    }}"""

    attempt = 0
    start = time.time()
    last_msg = ""
    while True:
        attempt += 1
        try:
            result = graphql(api_key, query)
            pod = (result.get("data") or {}).get("podFindAndDeployOnDemand")
            if pod:
                elapsed = time.time() - start
                loc = pod.get("machine", {}).get("location", "?")
                cost = pod.get("costPerHr", "?")
                print(
                    f"\n[GRABBED] Pod {pod['id']} at {loc} "
                    f"(${cost}/hr) after {attempt} attempts ({elapsed:.0f}s)"
                )
                return pod

            errors = result.get("errors", [])
            msg = errors[0]["message"][:50] if errors else "unknown error"
        except Exception as e:
            msg = str(e)[:50]

        elapsed = time.time() - start
        status = f"[{attempt}] {time.strftime('%H:%M:%S')} ({elapsed:.0f}s) {msg}"
        # Only print if message changed (avoid flooding terminal)
        if msg != last_msg:
            print(f"\r{status:<80}", end="", flush=True)
            last_msg = msg
        else:
            # Same message — just update attempt counter
            print(f"\r{status:<80}", end="", flush=True)

        time.sleep(interval)


def wait_for_ssh(
    api_key: str, pod_id: str, ssh_key: str, timeout: int = 600
) -> tuple[str, int]:
    """Wait for the pod's SSH port to accept connections."""
    query = f"""query {{
        pod(input: {{ podId: "{pod_id}" }}) {{
            runtime {{
                uptimeInSeconds
                ports {{ ip publicPort privatePort isIpPublic }}
            }}
        }}
    }}"""

    start = time.time()
    print(f"[SSH] Waiting for SSH on pod {pod_id}...", flush=True)
    while time.time() - start < timeout:
        try:
            result = graphql(api_key, query)
            pod_data = (result.get("data") or {}).get("pod") or {}
            runtime = pod_data.get("runtime") or {}
            ports = runtime.get("ports") or []
            for p in ports:
                if p.get("privatePort") == 22 and p.get("ip"):
                    ip = p["ip"]
                    port = p["publicPort"]
                    ret = subprocess.run(
                        [
                            "ssh",
                            "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=5",
                            "-o", "StrictHostKeyChecking=no",
                            "-p", str(port),
                            "-i", ssh_key,
                            f"root@{ip}",
                            "echo ok",
                        ],
                        capture_output=True,
                        timeout=10,
                    )
                    if ret.returncode == 0:
                        elapsed = time.time() - start
                        print(f"[SSH] Connected to {ip}:{port} ({elapsed:.0f}s)")
                        return ip, port
        except Exception:
            pass

        elapsed = time.time() - start
        print(
            f"\r[SSH] Waiting... ({elapsed:.0f}s / {timeout}s)    ",
            end="",
            flush=True,
        )
        time.sleep(5)

    raise TimeoutError(f"SSH not available after {timeout}s")


def ssh_cmd(ip: str, port: int, ssh_key: str, cmd: str, timeout: int = 30) -> str:
    """Run a command on the pod via SSH and return stdout."""
    result = subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-p", str(port),
            "-i", ssh_key,
            f"root@{ip}",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def scp_to_pod(
    ip: str, port: int, ssh_key: str, local_path: str, remote_path: str
) -> None:
    subprocess.run(
        [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-P", str(port),
            "-i", ssh_key,
            local_path,
            f"root@{ip}:{remote_path}",
        ],
        check=True,
        timeout=30,
    )


def deliver_creds(
    ip: str, port: int, ssh_key: str, creds: dict[str, str]
) -> None:
    """Write R2 credentials to /dev/shm/secure/creds on the pod."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".creds", delete=False) as f:
        for key, value in creds.items():
            if value:
                f.write(f'export {key}="{value}"\n')
        creds_path = f.name

    try:
        ssh_cmd(ip, port, ssh_key, "mkdir -p /dev/shm/secure && chmod 700 /dev/shm/secure")
        scp_to_pod(ip, port, ssh_key, creds_path, "/dev/shm/secure/creds")
        ssh_cmd(ip, port, ssh_key, "chmod 600 /dev/shm/secure/creds")
        print("[CREDS] Delivered to /dev/shm/secure/creds")
    finally:
        os.unlink(creds_path)


def deliver_runner_and_plan(
    ip: str,
    port: int,
    ssh_key: str,
    plan_file: str | None,
) -> None:
    """SCP the runner script and optional training plan to the pod."""
    runner_src = os.path.join(os.path.dirname(__file__), "runpod_runner.sh")
    if not os.path.exists(runner_src):
        raise FileNotFoundError(f"Runner script not found: {runner_src}")

    scp_to_pod(ip, port, ssh_key, runner_src, "/opt/protenix-tools/runpod_runner.sh")
    ssh_cmd(ip, port, ssh_key, "chmod +x /opt/protenix-tools/runpod_runner.sh")
    print("[RUNNER] Uploaded runpod_runner.sh")

    if plan_file:
        scp_to_pod(ip, port, ssh_key, plan_file, "/data/training_plan.json")
        print(f"[PLAN] Uploaded {plan_file} → /data/training_plan.json")


def launch_runner(
    ip: str,
    port: int,
    ssh_key: str,
    plan_file: str | None,
) -> None:
    """Launch the runner in background on the pod via nohup."""
    plan_arg = "--plan /data/training_plan.json" if plan_file else ""
    cmd = (
        f"nohup /opt/protenix-tools/runpod_runner.sh {plan_arg} "
        f">/data/runner.log 2>&1 & echo $!"
    )
    pid = ssh_cmd(ip, port, ssh_key, cmd)
    print(f"[RUNNER] Launched (PID {pid})")


def play_alert_sound() -> None:
    """Play an audible alert through speakers. Best-effort, tries multiple methods."""
    try:
        if sys.platform == "linux":
            sounds = [
                "/usr/share/sounds/freedesktop/stereo/complete.oga",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
                "/usr/share/sounds/gnome/default/alerts/glass.ogg",
                "/usr/share/sounds/ubuntu/stereo/system-ready.ogg",
            ]
            # pw-play (PipeWire), paplay (PulseAudio), aplay (ALSA fallback)
            players = ["pw-play", "paplay", "aplay"]
            for sound in sounds:
                if not os.path.exists(sound):
                    continue
                for player in players:
                    if subprocess.run(
                        ["which", player], capture_output=True
                    ).returncode == 0:
                        for _ in range(3):
                            subprocess.run(
                                [player, sound], timeout=5, capture_output=True,
                            )
                        # Also speak it for clarity
                        if subprocess.run(
                            ["which", "spd-say"], capture_output=True
                        ).returncode == 0:
                            subprocess.run(
                                ["spd-say", "-w",
                                 "RunPod acquired. Training launching."],
                                timeout=10, capture_output=True,
                            )
                        return
            # Sound files missing — try speech only
            if subprocess.run(
                ["which", "spd-say"], capture_output=True
            ).returncode == 0:
                subprocess.run(
                    ["spd-say", "-w",
                     "RunPod acquired. Training launching."],
                    timeout=10, capture_output=True,
                )
                return
        elif sys.platform == "darwin":
            subprocess.run(
                ["say", "RunPod acquired. Training launching."],
                timeout=10, capture_output=True,
            )
            return
    except Exception:
        pass
    # Last resort: terminal bell
    print("\a\a\a", flush=True)


def notify_desktop(title: str, body: str) -> None:
    """Send a desktop notification + audible alert. Best-effort."""
    try:
        if sys.platform == "linux":
            subprocess.run(
                ["notify-send", "-u", "critical", title, body],
                timeout=5,
                capture_output=True,
            )
        elif sys.platform == "darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{body}" with title "{title}"',
                ],
                timeout=5,
                capture_output=True,
            )
    except Exception:
        pass
    play_alert_sound()


def main() -> None:
    default_env = _find_env_file()

    parser = argparse.ArgumentParser(
        description="Poll RunPod for GPU availability and auto-launch training"
    )
    parser.add_argument(
        "--template-id", default="2ei3isdcxc",
        help="RunPod template ID (default: Protenix_5090)",
    )
    parser.add_argument(
        "--gpu-type", default="NVIDIA GeForce RTX 5090",
        help="GPU type ID string",
    )
    parser.add_argument(
        "--cloud-type", default="SECURE",
        choices=["SECURE", "COMMUNITY", "ALL"],
    )
    parser.add_argument("--volume-gb", type=int, default=250)
    parser.add_argument("--name", default="Protenix-5090-devel")
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="Poll interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--plan",
        help="Training plan JSON file (chain of runs)",
    )
    parser.add_argument(
        "--env-file", default=default_env,
        help="Path to .env file with R2 credentials",
    )
    parser.add_argument(
        "--ssh-key", default="~/.ssh/id_ed25519",
    )
    parser.add_argument(
        "--ssh-timeout", type=int, default=900,
        help="Seconds to wait for SSH after pod acquisition (default: 900)",
    )
    parser.add_argument(
        "--no-launch", action="store_true",
        help="Grab pod but don't launch runner",
    )
    args = parser.parse_args()
    args.ssh_key = os.path.expanduser(args.ssh_key)

    # ---- Validate prerequisites ----
    api_key = get_api_key()
    r2_creds = get_r2_creds(args.env_file)

    missing = [k for k, v in r2_creds.items() if not v]
    if missing:
        print(f"[ERROR] Missing R2 credentials: {', '.join(missing)}")
        print("Provide via --env-file, CLOUDFLARE_R2_* env vars, or ~/.aws/credentials")
        sys.exit(1)

    if args.plan and not os.path.exists(args.plan):
        print(f"[ERROR] Training plan not found: {args.plan}")
        sys.exit(1)

    runner_path = os.path.join(os.path.dirname(__file__), "runpod_runner.sh")
    if not args.no_launch and not os.path.exists(runner_path):
        print(f"[ERROR] Runner script not found: {runner_path}")
        sys.exit(1)

    # ---- Phase 1: Grab a pod ----
    print(f"[GRABBER] Polling for {args.gpu_type} ({args.cloud_type}) every {args.interval}s")
    print(f"[GRABBER] Template: {args.template_id}, Volume: {args.volume_gb} GB")
    if args.env_file:
        print(f"[GRABBER] Creds from: {args.env_file}")
    print(f"[GRABBER] Started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    pod = poll_for_pod(
        api_key, args.template_id, args.gpu_type, args.cloud_type,
        args.volume_gb, args.name, args.interval,
    )
    pod_id = pod["id"]

    notify_desktop(
        "RunPod Pod Acquired!",
        f"Pod {pod_id} at {pod.get('machine', {}).get('location', '?')}",
    )

    if args.no_launch:
        print(f"\n[DONE] Pod {pod_id} acquired. SSH will be available shortly.")
        print(f"  runpodctl ssh {pod_id}")
        sys.exit(0)

    # ---- Phase 2: Wait for SSH ----
    ip, port = wait_for_ssh(api_key, pod_id, args.ssh_key, timeout=args.ssh_timeout)

    # ---- Phase 3: Deliver credentials ----
    deliver_creds(ip, port, args.ssh_key, r2_creds)

    # ---- Phase 4: Upload runner + plan ----
    deliver_runner_and_plan(ip, port, args.ssh_key, args.plan)

    # ---- Phase 5: Launch runner ----
    launch_runner(ip, port, args.ssh_key, args.plan)

    notify_desktop("Training Launched!", f"Pod {pod_id} — runner started")

    # ---- Print summary ----
    print()
    print("=" * 70)
    print(f"  Pod ID:     {pod_id}")
    print(f"  Location:   {pod.get('machine', {}).get('location', '?')}")
    print(f"  Cost:       ${pod.get('costPerHr', '?')}/hr")
    print(f"  SSH:        ssh root@{ip} -p {port} -i {args.ssh_key}")
    print()
    print(f"  Runner log: ssh root@{ip} -p {port} -i {args.ssh_key} 'tail -f /data/runner.log'")
    print(f"  Train log:  ssh root@{ip} -p {port} -i {args.ssh_key} 'tail -f /data/training_output/*/training.log'")
    print()
    print(f"  R2 telemetry: s3://vh-protenix-training/telemetry/{pod_id}/status.json")
    print("=" * 70)


if __name__ == "__main__":
    main()
