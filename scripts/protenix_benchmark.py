#!/usr/bin/env python3
"""
Protenix Performance Benchmarking Script
=========================================
Monitors GPU VRAM, RAM, CPU usage during Protenix inference.
Produces structured performance data for cross-GPU comparison.

Usage:
    python3 protenix_benchmark.py --input_json INPUT.json --dump_dir OUTPUT_DIR [--seeds 101]
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


class ResourceMonitor:
    """Monitors GPU/CPU/RAM usage in a background thread."""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        while not self._stop.is_set():
            sample = self._take_sample()
            if sample:
                self.samples.append(sample)
            self._stop.wait(self.interval)

    def _take_sample(self):
        try:
            ts = time.time()

            # GPU metrics via nvidia-smi
            gpu_result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,utilization.memory,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            gpu_parts = [x.strip() for x in gpu_result.stdout.strip().split(",")]

            # RAM metrics
            with open("/proc/meminfo") as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1])  # kB

            total_ram_mb = meminfo.get("MemTotal", 0) / 1024
            free_ram_mb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) / 1024
            used_ram_mb = total_ram_mb - free_ram_mb

            # CPU usage (instantaneous from /proc/stat)
            cpu_pct = self._get_cpu_percent()

            return {
                "timestamp": ts,
                "gpu_vram_used_mb": int(gpu_parts[0]) if len(gpu_parts) > 0 else 0,
                "gpu_vram_total_mb": int(gpu_parts[1]) if len(gpu_parts) > 1 else 0,
                "gpu_util_pct": int(gpu_parts[2]) if len(gpu_parts) > 2 else 0,
                "gpu_mem_util_pct": int(gpu_parts[3]) if len(gpu_parts) > 3 else 0,
                "gpu_temp_c": int(gpu_parts[4]) if len(gpu_parts) > 4 else 0,
                "gpu_power_w": float(gpu_parts[5]) if len(gpu_parts) > 5 else 0,
                "ram_used_mb": int(used_ram_mb),
                "ram_total_mb": int(total_ram_mb),
                "cpu_pct": cpu_pct,
            }
        except Exception as e:
            return None

    def _get_cpu_percent(self):
        """Get CPU usage percentage."""
        try:
            result = subprocess.run(
                ["grep", "cpu ", "/proc/stat"],
                capture_output=True, text=True, timeout=2
            )
            parts = result.stdout.strip().split()
            if len(parts) >= 5:
                idle = int(parts[4])
                total = sum(int(x) for x in parts[1:])
                if not hasattr(self, '_last_cpu'):
                    self._last_cpu = (idle, total)
                    return 0.0
                d_idle = idle - self._last_cpu[0]
                d_total = total - self._last_cpu[1]
                self._last_cpu = (idle, total)
                if d_total > 0:
                    return round((1.0 - d_idle / d_total) * 100, 1)
            return 0.0
        except:
            return 0.0

    def get_summary(self):
        """Return summary statistics from collected samples."""
        if not self.samples:
            return {}

        def stat(key):
            vals = [s[key] for s in self.samples if key in s and s[key] is not None]
            if not vals:
                return {"min": 0, "max": 0, "avg": 0}
            return {
                "min": min(vals),
                "max": max(vals),
                "avg": round(sum(vals) / len(vals), 1),
            }

        return {
            "num_samples": len(self.samples),
            "duration_sec": round(self.samples[-1]["timestamp"] - self.samples[0]["timestamp"], 1) if len(self.samples) > 1 else 0,
            "gpu_vram_used_mb": stat("gpu_vram_used_mb"),
            "gpu_vram_total_mb": self.samples[0].get("gpu_vram_total_mb", 0),
            "gpu_util_pct": stat("gpu_util_pct"),
            "gpu_mem_util_pct": stat("gpu_mem_util_pct"),
            "gpu_temp_c": stat("gpu_temp_c"),
            "gpu_power_w": stat("gpu_power_w"),
            "ram_used_mb": stat("ram_used_mb"),
            "ram_total_mb": self.samples[0].get("ram_total_mb", 0),
            "cpu_pct": stat("cpu_pct"),
        }


def get_gpu_info():
    """Get static GPU information."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,pcie.link.gen.current,pcie.link.width.current",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        parts = [x.strip() for x in result.stdout.strip().split(",")]
        return {
            "gpu_name": parts[0] if len(parts) > 0 else "Unknown",
            "driver_version": parts[1] if len(parts) > 1 else "Unknown",
            "vram_total_mb": parts[2] if len(parts) > 2 else "Unknown",
            "pcie_gen": parts[3] if len(parts) > 3 else "Unknown",
            "pcie_width": parts[4] if len(parts) > 4 else "Unknown",
        }
    except:
        return {"gpu_name": "Unknown"}


def get_system_info():
    """Get system information."""
    info = {}
    try:
        info["hostname"] = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    except:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
        model_names = [l.split(":")[1].strip() for l in cpuinfo.split("\n") if "model name" in l]
        info["cpu_model"] = model_names[0] if model_names else "Unknown"
        info["cpu_count"] = len(model_names)
    except:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    info["ram_total_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except:
        pass
    try:
        result = subprocess.run(["python3", "--version"], capture_output=True, text=True)
        info["python_version"] = result.stdout.strip()
    except:
        pass
    try:
        result = subprocess.run(["python3", "-c", "import torch; print(torch.__version__)"],
                              capture_output=True, text=True)
        info["pytorch_version"] = result.stdout.strip()
    except:
        pass
    try:
        result = subprocess.run(["nvcc", "--version"], capture_output=True, text=True)
        for line in result.stdout.split("\n"):
            if "release" in line:
                info["cuda_version"] = line.strip()
                break
    except:
        pass
    return info


def run_protenix_inference(input_json, dump_dir, seeds="101", use_msa="false", extra_args=None):
    """Run Protenix inference and capture timing."""
    cmd = [
        "python3", "runner/inference.py",
        "--input_json_path", input_json,
        "--dump_dir", dump_dir,
        "--seeds", seeds,
        "--use_msa", use_msa,
    ]
    if extra_args:
        cmd.extend(extra_args)

    print(f"\n{'='*60}")
    print(f"Running: {' '.join(cmd)}")
    print(f"{'='*60}\n")

    start = time.time()
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd="/workspace"
    )

    output_lines = []
    for line in process.stdout:
        print(line, end="")
        output_lines.append(line)

    process.wait()
    elapsed = time.time() - start

    return {
        "return_code": process.returncode,
        "elapsed_sec": round(elapsed, 2),
        "output": "".join(output_lines),
    }


def parse_protenix_output(output_text):
    """Extract timing and metrics from Protenix output."""
    metrics = {}
    for line in output_text.split("\n"):
        if "Forward time:" in line:
            try:
                metrics["forward_time_sec"] = float(line.split("Forward time:")[1].strip().rstrip("s"))
            except:
                pass
        if "num_token" in line:
            try:
                metrics["num_tokens"] = int(line.split("num_token:")[1].strip().split()[0].rstrip(","))
            except:
                pass
        if "num_atom" in line:
            try:
                metrics["num_atoms"] = int(line.split("num_atom:")[1].strip().split()[0].rstrip(","))
            except:
                pass
        if "Total number of parameters" in line:
            try:
                val = line.split(":")[1].strip()
                metrics["model_params"] = val
            except:
                pass
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Protenix Performance Benchmark")
    parser.add_argument("--input_json", required=True, help="Path to Protenix input JSON")
    parser.add_argument("--dump_dir", default="/workspace/benchmark_output", help="Output directory")
    parser.add_argument("--seeds", default="101", help="Seeds (comma-separated)")
    parser.add_argument("--use_msa", default="false", help="Use MSA")
    parser.add_argument("--monitor_interval", type=float, default=0.5, help="Resource monitoring interval (sec)")
    parser.add_argument("--output_report", default=None, help="Path for JSON benchmark report")
    parser.add_argument("--label", default=None, help="Label for this benchmark run")
    args = parser.parse_args()

    # Collect system info
    print("Collecting system information...")
    sys_info = get_system_info()
    gpu_info = get_gpu_info()
    print(f"  GPU: {gpu_info.get('gpu_name', 'Unknown')}")
    print(f"  CPU: {sys_info.get('cpu_model', 'Unknown')} x{sys_info.get('cpu_count', '?')}")
    print(f"  RAM: {sys_info.get('ram_total_gb', '?')} GB")

    # Read input to get job info
    with open(args.input_json) as f:
        input_data = json.load(f)
    job_info = []
    for job in input_data:
        chains = []
        for seq in job.get("sequences", []):
            for entity_type in ["proteinChain", "dnaSequence", "rnaSequence", "ligand", "ion"]:
                if entity_type in seq:
                    chains.append({
                        "type": entity_type,
                        "id": seq[entity_type].get("id", "?"),
                        "length": len(seq[entity_type].get("sequence", "")),
                        "count": seq[entity_type].get("count", 1),
                    })
        job_info.append({"name": job.get("name", "unnamed"), "chains": chains})

    print(f"\nJobs to benchmark: {len(job_info)}")
    for ji in job_info:
        total_residues = sum(c["length"] * c["count"] for c in ji["chains"])
        print(f"  {ji['name']}: {len(ji['chains'])} chains, {total_residues} total residues")

    # Start monitoring
    monitor = ResourceMonitor(interval=args.monitor_interval)
    monitor.start()

    # Run inference
    run_result = run_protenix_inference(
        args.input_json, args.dump_dir, args.seeds, args.use_msa
    )

    # Stop monitoring
    monitor.stop()

    # Parse output
    protenix_metrics = parse_protenix_output(run_result["output"])
    resource_summary = monitor.get_summary()

    # Build report
    report = {
        "benchmark_timestamp": datetime.now().isoformat(),
        "label": args.label or f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "system": {**sys_info, **gpu_info},
        "input": {
            "json_path": args.input_json,
            "seeds": args.seeds,
            "use_msa": args.use_msa,
            "jobs": job_info,
        },
        "timing": {
            "total_elapsed_sec": run_result["elapsed_sec"],
            "forward_time_sec": protenix_metrics.get("forward_time_sec"),
            "overhead_sec": round(run_result["elapsed_sec"] - protenix_metrics.get("forward_time_sec", 0), 2) if protenix_metrics.get("forward_time_sec") else None,
        },
        "model": {
            "num_tokens": protenix_metrics.get("num_tokens"),
            "num_atoms": protenix_metrics.get("num_atoms"),
            "parameters": protenix_metrics.get("model_params"),
        },
        "resources": resource_summary,
        "return_code": run_result["return_code"],
    }

    # Print summary
    print(f"\n{'='*60}")
    print("BENCHMARK RESULTS")
    print(f"{'='*60}")
    print(f"GPU:                  {gpu_info.get('gpu_name', 'Unknown')}")
    print(f"Total elapsed:        {run_result['elapsed_sec']:.1f}s")
    if protenix_metrics.get("forward_time_sec"):
        print(f"Forward time:         {protenix_metrics['forward_time_sec']:.1f}s")
        overhead = run_result['elapsed_sec'] - protenix_metrics['forward_time_sec']
        print(f"Overhead (load+init): {overhead:.1f}s")
    print(f"VRAM peak:            {resource_summary.get('gpu_vram_used_mb', {}).get('max', 'N/A')} / {resource_summary.get('gpu_vram_total_mb', 'N/A')} MB")
    print(f"GPU util peak:        {resource_summary.get('gpu_util_pct', {}).get('max', 'N/A')}%")
    print(f"GPU temp peak:        {resource_summary.get('gpu_temp_c', {}).get('max', 'N/A')}°C")
    print(f"GPU power peak:       {resource_summary.get('gpu_power_w', {}).get('max', 'N/A')}W")
    print(f"RAM peak:             {resource_summary.get('ram_used_mb', {}).get('max', 'N/A')} / {resource_summary.get('ram_total_mb', 'N/A')} MB")
    print(f"CPU util peak:        {resource_summary.get('cpu_pct', {}).get('max', 'N/A')}%")
    print(f"Tokens:               {protenix_metrics.get('num_tokens', 'N/A')}")
    print(f"Atoms:                {protenix_metrics.get('num_atoms', 'N/A')}")
    print(f"{'='*60}")

    # Save report
    report_path = args.output_report or os.path.join(args.dump_dir, "benchmark_report.json")
    os.makedirs(os.path.dirname(report_path) if os.path.dirname(report_path) else ".", exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to: {report_path}")

    # Save raw samples as CSV
    samples_path = report_path.replace(".json", "_samples.csv")
    if monitor.samples:
        with open(samples_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=monitor.samples[0].keys())
            writer.writeheader()
            writer.writerows(monitor.samples)
        print(f"Raw samples saved to: {samples_path}")

    return report


if __name__ == "__main__":
    main()
