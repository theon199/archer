#!/usr/bin/env python3
"""Bounded read-only provenance and actual local inference/resource snapshot."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/checkpoint-25-local-model.json"
MODEL = "gemma4:26b"
DIGEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"
BASE = "http://127.0.0.1:11434"


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def file_identity(path):
    try:
        before = path.stat()
        sha = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                sha.update(chunk)
        after = path.stat()
        return {
            "bytes": before.st_size,
            "sha256": sha.hexdigest(),
            "stable_during_hash": (before.st_ino, before.st_mtime_ns, before.st_size)
            == (after.st_ino, after.st_mtime_ns, after.st_size),
        }
    except OSError as error:
        return {"error_type": type(error).__name__}


def command(arguments):
    try:
        r = subprocess.run(
            arguments, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, check=False
        )
        return {"exit_code": r.returncode, "stdout": r.stdout[:16384]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error_type": type(e).__name__}


def memory():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, raw = line.split(":", 1)
        if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            values[name + "_bytes"] = int(raw.split()[0]) * 1024
    return values


async def main():
    result = {
        "state": "in_progress",
        "checked_at": datetime.now(UTC).isoformat(),
        "provenance_complete": False,
        "performance_complete": False,
    }
    EVIDENCE.write_text(json.dumps(result) + "\n")
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=15) as client:
        version = (await client.get("/api/version")).json()
        tags = (await client.get("/api/tags")).json()
        selected = next(m for m in tags["models"] if m["name"] == MODEL)
        if selected["digest"] != DIGEST:
            raise RuntimeError("Cached model digest mismatch")
        response = await client.post("/api/show", json={"model": MODEL, "verbose": True})
        response.raise_for_status()
        show = response.json()
        info = show["model_info"]
        arrays = {
            k: {"items": len(v), "canonical_json_sha256": canonical_hash(v)}
            for k, v in info.items()
            if isinstance(v, list)
        }
        scalar = {k: v for k, v in info.items() if not isinstance(v, list)}
        modelfile = show["modelfile"]
        blob_ids = sorted(set(re.findall(r"/blobs/(sha256-[a-f0-9]{64})", modelfile)))
        model_root = Path("/usr/share/ollama/.ollama/models")
        artifacts = {
            "manifest": file_identity(model_root / "manifests/registry.ollama.ai/library/gemma4/26b"),
            "blobs": {name: file_identity(model_root / "blobs" / name) for name in blob_ids},
        }
        binary = file_identity(Path("/usr/local/bin/ollama"))
        libraries = {}
        hashed = 0
        library_root = Path("/usr/local/lib/ollama")
        for path in sorted(library_root.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.name.endswith("LICENSE"):
                continue
            if path.stat().st_size + hashed > 3 * 1024**3:
                raise RuntimeError("Installed engine library hash byte budget")
            hashed += path.stat().st_size
            libraries[str(path.relative_to(library_root))] = file_identity(path)
        proc = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry / "comm").read_text().strip() != "ollama":
                    continue
                args = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
                if len(args) > 1 and args[1] == "serve":
                    row = {"pid": int(entry.name), "argv_prefix": args[:2]}
                    try:
                        row["exe"] = os.readlink(entry / "exe")
                        row["running_exe"] = file_identity(entry / "exe")
                    except OSError as e:
                        row["running_exe_error"] = type(e).__name__
                    proc.append(row)
            except OSError:
                continue
        buildinfo = command(["readelf", "-p", ".go.buildinfo", "/usr/local/bin/ollama"])
        buildinfo_text = buildinfo.get("stdout", "")
        result.update(
            {
                "engine_api": version,
                "model": selected,
                "model_scalar_metadata": scalar,
                "model_array_metadata": arrays,
                "template": show["template"],
                "template_sha256": hashlib.sha256(show["template"].encode()).hexdigest(),
                "parameters": show["parameters"],
                "capabilities": show["capabilities"],
                "modelfile_sha256": hashlib.sha256(modelfile.encode()).hexdigest(),
                "modelfile_recipe_lines": [
                    line
                    for line in modelfile.splitlines()
                    if line.startswith(("FROM ", "DRAFT ", "TEMPLATE ", "RENDERER ", "PARSER ", "PARAMETER "))
                ],
                "license_sha256": hashlib.sha256(show.get("license", "").encode()).hexdigest(),
                "api_model_info_sha256": canonical_hash(info),
                "artifact_verification": artifacts,
                "installed_engine": binary,
                "installed_library_bytes_hashed": hashed,
                "installed_libraries": libraries,
                "serving_process": proc,
                "go_build_metadata": {
                    "sha256": hashlib.sha256(buildinfo_text.encode()).hexdigest(),
                    "selected_lines": [
                        line.strip()
                        for line in buildinfo_text.splitlines()
                        if any(key in line for key in ["go1.", "build^I", "mod^I", "vcs."])
                    ],
                },
                "host": {
                    "architecture": platform.machine(),
                    "kernel": platform.release(),
                    "os_release": Path("/etc/os-release").read_text(),
                    "memory_before": memory(),
                    "gpu": command(
                        [
                            "nvidia-smi",
                            "--query-gpu=name,uuid,driver_version,compute_cap,memory.total,memory.used",
                            "--format=csv,noheader",
                        ]
                    ),
                    "driver_module": Path("/proc/driver/nvidia/version").read_text(),
                    "listener": command(["ss", "-ltn", "( sport = :11434 )"]),
                },
            }
        )
        result["provenance_missing_evidence"] = [
            "Independent readable manifest/blob checksums and equality to API model digest",
            "Running server executable identity binding and mapped runtime/CUDA libraries (process permission boundary)",
            "Publisher/source-build authenticity for installed executable and libraries; version-matched source alone is insufficient",
        ]
        EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        before = (await client.get("/api/ps")).json()
        result["residency_before"] = before
        if any(m.get("name") != MODEL for m in before.get("models", [])):
            result["performance_skipped"] = "Other loaded model present; no eviction/reload attempted"
            EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
            return 1
        samples = []
        stop = asyncio.Event()

        async def sample():
            while not stop.is_set():
                record = {"monotonic_seconds": time.monotonic(), "memory": memory()}
                try:
                    record["residency"] = (await client.get("/api/ps")).json()
                except httpx.HTTPError:
                    record["residency_error"] = True
                record["gpu"] = await asyncio.to_thread(
                    command,
                    ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,temperature.gpu", "--format=csv,noheader"],
                )
                samples.append(record)
                try:
                    await asyncio.wait_for(stop.wait(), 0.25)
                except TimeoutError:
                    pass

        sampler = asyncio.create_task(sample())
        runs = []
        cases = [
            ("short_initial", "Reply with exactly LOCAL_PROVENANCE_OK.", "LOCAL_PROVENANCE_OK"),
            ("short_warm", "Reply with exactly LOCAL_PROVENANCE_OK.", "LOCAL_PROVENANCE_OK"),
            (
                "errand_arithmetic",
                "Synthetic planning task. Start at 09:00. Groceries take 30 minutes, pharmacy 20 minutes, library 15 minutes. There are exactly two 10-minute travel legs between stops, no other travel. Calculate total minutes and ending time. Reply only total_minutes=<number>; end_time=<HH:MM>.",
                None,
            ),
            (
                "source_extraction",
                "Use only this synthetic source: Grocery closes 18:00. Library closes 17:00. Pharmacy closes 19:00. Which closes first? Reply only its name.",
                "Library",
            ),
        ]
        try:
            for name, prompt, expected in cases:
                payload = {
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                    "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 256},
                }
                start = time.monotonic()
                first = None
                output = ""
                done = None
                frames = 0
                async with asyncio.timeout(90):
                    async with client.stream("POST", "/api/chat", json=payload, timeout=90) as stream:
                        stream.raise_for_status()
                        async for line in stream.aiter_lines():
                            if not line:
                                continue
                            frame = json.loads(line)
                            frames += 1
                            if "error" in frame:
                                raise RuntimeError("Real provider error frame")
                            content = frame.get("message", {}).get("content", "")
                            if content and first is None:
                                first = time.monotonic() - start
                            output += content
                            if len(output) > 16000 or frames > 1024:
                                raise RuntimeError("Inference observation limits")
                            if frame.get("done") is True:
                                done = frame
                quality = (
                    expected in output
                    if expected
                    else bool(
                        re.search(r"total_minutes\s*=\s*85\b", output) and re.search(r"end_time\s*=\s*10:25\b", output)
                    )
                )
                metrics = (
                    {
                        k: done.get(k)
                        for k in [
                            "done_reason",
                            "total_duration",
                            "load_duration",
                            "prompt_eval_count",
                            "prompt_eval_cached_count",
                            "prompt_eval_duration",
                            "eval_count",
                            "eval_duration",
                        ]
                    }
                    if done
                    else {}
                )
                if metrics.get("eval_duration"):
                    metrics["reported_output_tokens_per_second"] = (
                        metrics["eval_count"] * 1e9 / metrics["eval_duration"]
                    )
                runs.append(
                    {
                        "case": name,
                        "request_sha256": canonical_hash(payload),
                        "prompt": prompt,
                        "response": output,
                        "wall_seconds": time.monotonic() - start,
                        "first_content_seconds": first,
                        "completion_observed": done is not None,
                        "quality_check": quality,
                        "metrics": metrics,
                    }
                )
                result["runs"] = runs
                EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        finally:
            stop.set()
            await sampler
        result.update(
            {
                "runs": runs,
                "resource_samples": samples,
                "performance_complete": all(r["completion_observed"] for r in runs),
                "quality_checks_passed": all(r["quality_check"] for r in runs),
                "residency_after": (await client.get("/api/ps")).json(),
                "memory_after": memory(),
                "state": "finished",
                "checked_at": datetime.now(UTC).isoformat(),
                "scope": "Real local Ollama API snapshot and four synthetic bounded generation probes. Installed readable engine bytes hashed; protected model files/running executable remain unverified. Direct engine timings/resources, not full Archer latency/load/quality certification or process attribution of all host memory. No unload/service/driver/network/model update.",
            }
        )
        EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "state": result["state"],
                    "provenance_complete": False,
                    "performance_complete": result["performance_complete"],
                    "quality_checks_passed": result["quality_checks_passed"],
                    "runs": runs,
                    "resource_samples": len(samples),
                    "installed_library_bytes_hashed": hashed,
                    "missing_evidence": result["provenance_missing_evidence"],
                }
            )
        )
        return 0 if result["performance_complete"] and result["quality_checks_passed"] else 1


async def planning_follow_up():
    initial = json.loads(EVIDENCE.read_text())
    original = next(run for run in initial["runs"] if run["case"] == "errand_arithmetic")
    target = ROOT / "docs/evidence/checkpoint-25-planning-follow-up.json"
    target.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=45) as client:
        tags = (await client.get("/api/tags")).json()
        if not any(m["name"] == MODEL and m["digest"] == DIGEST for m in tags["models"]):
            raise RuntimeError("Pinned model unavailable")
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": original["prompt"]}],
            "stream": True,
            "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 1024},
        }
        output = ""
        thinking_chars = 0
        first_content = None
        start = time.monotonic()
        done = None
        async with asyncio.timeout(45):
            async with client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    frame = json.loads(line)
                    if "error" in frame:
                        raise RuntimeError("Provider error frame")
                    text = frame.get("message", {}).get("content", "")
                    thinking_chars += len(frame.get("message", {}).get("thinking", ""))
                    if text and first_content is None:
                        first_content = time.monotonic() - start
                    output += text
                    if len(output) > 16000:
                        raise RuntimeError("Output limit")
                    if frame.get("done") is True:
                        done = frame
        passed = bool(
            done
            and done.get("done_reason") == "stop"
            and re.search(r"total_minutes\s*=\s*85\b", output)
            and re.search(r"end_time\s*=\s*10:25\b", output)
        )
        result = {
            "passed": passed,
            "checked_at": datetime.now(UTC).isoformat(),
            "same_prompt_as_initial": payload["messages"][0]["content"] == original["prompt"],
            "initial_num_predict": 256,
            "follow_up_num_predict": 1024,
            "initial_result_preserved": "checkpoint-25-local-model.json",
            "request_sha256": canonical_hash(payload),
            "output": output,
            "thinking_characters_observed": thinking_chars,
            "first_content_seconds": first_content,
            "wall_seconds": time.monotonic() - start,
            "metrics": {
                k: done.get(k)
                for k in [
                    "done_reason",
                    "total_duration",
                    "load_duration",
                    "prompt_eval_count",
                    "eval_count",
                    "eval_duration",
                ]
            }
            if done
            else {},
            "scope": "One real local same-prompt follow-up with larger prediction budget; does not erase initial capped quality failure or establish general quality.",
        }
        target.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planning-follow-up", action="store_true")
    args = parser.parse_args()
    try:
        status = asyncio.run(asyncio.wait_for(planning_follow_up() if args.planning_follow_up else main(), timeout=360))
    except Exception as error:  # noqa: BLE001 - Preserve sanitized partial evidence.
        failure_target = (
            ROOT / "docs/evidence/checkpoint-25-planning-follow-up.json" if args.planning_follow_up else EVIDENCE
        )
        old = json.loads(failure_target.read_text()) if failure_target.exists() else {}
        old.update(state="failed", error_type=type(error).__name__, checked_at=datetime.now(UTC).isoformat())
        failure_target.write_text(json.dumps(old, indent=2) + "\n")
        print(json.dumps({"state": "failed", "error_type": type(error).__name__}))
        status = 1
    raise SystemExit(status)
