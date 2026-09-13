"""Persistent local gate jobs and nonblocking, same-store resource exclusion.

Lock inodes are stable; ownership metadata is a separate atomic JSON file. A
released OS lock never proves a recorded RUNNING job has ended. Recovery observes
local process identities and never terminates a process. Separate stores are not
coordinated: the host must serialize shared devices/services across those stores.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import ctypes
import fcntl
import functools
import json
import os
from pathlib import Path
import socket
import stat
import sys
import uuid

_CURRENT = contextvars.ContextVar("delivery_gate_job", default=None)
_ACTIVE = {"IMPLEMENTING", "VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}


def _engine():
    import run_engine
    return run_engine


def _observe(pid: int) -> dict:
    """Inspect start identity, parent and zombie state without executing ps."""
    e = _engine()
    e.require(type(pid) is int and pid > 0, "invalid_process", "A process ID must be a positive integer.")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"pid": pid, "alive": False, "identity": None}
    except PermissionError as exc:
        raise e.RunError("process_unknown", "Process liveness is not observable; preserve the job.") from exc
    if sys.platform == "darwin":
        # Darwin's proc_bsdinfo has a stable 136-byte public ABI. Only its PID,
        # parent, state and microsecond start time are retained, never arguments.
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(136)
        size = library.proc_pidinfo(pid, 3, 0, buffer, len(buffer))
        if size != len(buffer):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return {"pid": pid, "alive": False, "identity": None}
            raise e.RunError("process_unknown", "The host cannot inspect the process start identity; preserve the job.")
        number = lambda start, end: int.from_bytes(buffer.raw[start:end], sys.byteorder)
        e.require(number(12, 16) == pid, "process_identity_drift", "Observed process identity changed during inspection.")
        return {"pid": pid, "alive": number(4, 8) != 5, "parent": number(16, 20),
                "identity": {"pid": pid, "start_seconds": number(120, 128), "start_microseconds": number(128, 136)}}
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return {"pid": pid, "alive": fields[0] not in {"Z", "X"}, "parent": int(fields[1]),
                    "identity": {"pid": pid, "start_ticks": int(fields[19]), "boot": boot}}
        except FileNotFoundError:
            return {"pid": pid, "alive": False, "identity": None}
        except (OSError, ValueError, IndexError) as exc:
            raise e.RunError("process_unknown", "The host cannot inspect process identity; preserve the job.") from exc
    raise e.RunError("process_unknown", "Local process identity inspection is supported on macOS and Linux only.")


def _resources(run: dict, task_id, case_id, gate_index) -> tuple[str, list[str]]:
    e = _engine()
    if task_id:
        task = e.task_plan(run, task_id)
        e.require(type(gate_index) is int and 0 <= gate_index < len(task["gates"]), "unknown_gate", "Select a planned task gate.")
        key, values = f"{task_id}:{gate_index}", task["resources"]
        e.require(case_id is None, "invalid_gate", "Select either a task gate or an integrated case.")
    else:
        case = next((entry for entry in run["plan"]["verification"] if entry["id"] == case_id), None)
        e.require(case, "unknown_case", "Select a planned integrated case.")
        key = case["id"]
        # An integrated case without its own declarations conservatively holds
        # every planned task resource; an explicit list is the narrower contract.
        values = case.get("resources", [r for task in run["plan"]["tasks"] for r in task["resources"]])
    e.require(isinstance(values, list) and all(isinstance(r, str) and r.strip() for r in values), "invalid_resource", "Gate resources must be named strings.")
    return key, sorted({r.strip() for r in values})


def _resource_dir(run: dict) -> Path:
    e = _engine()
    root = Path(run["root"])
    e.require(root.parent.name == "runs" and root.name == run["id"], "invalid_run", "Cannot establish the run's resource store.")
    directory = root.parents[2] / ".resources"
    e.require(not directory.is_symlink(), "symlink_state", "Refusing a symlink resource directory.")
    checkouts = [Path(item["worktree"]).resolve() for item in e.git_ops._worktrees(run["primary"])]
    checkouts.append(Path(e.git_ops.inspect_repo(run["primary"])["common_dir"]).resolve())
    e.require(not any(e.inside(directory.resolve(), checkout) for checkout in checkouts), "artifact_in_repo", "Resource locks must be outside every checkout and Git metadata directory.")
    directory.mkdir(exist_ok=True)
    return directory


def _keys(root: str, key: str, resources: list[str]) -> list[str]:
    e = _engine()
    return sorted([e.digest({"gate": key, "run": root})] + [e.digest({"resource": r}) for r in resources])


@contextlib.contextmanager
def _locks(directory: Path, keys: list[str]):
    e = _engine()
    handles = []
    try:
        for key in keys:
            path = directory / (key + ".lock")
            e.require(not path.is_symlink(), "symlink_state", "Refusing a symlink resource lock.")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            handles.append(fd)
            e.require(stat.S_ISREG(os.fstat(fd).st_mode), "invalid_resource_lock", "A resource lock must be a regular file.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise e.RunError("resource_in_use", "A gate holds this gate key or exclusive resource; wait for its actual result.") from exc
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _metadata(directory: Path, key: str):
    e = _engine()
    path = directory / (key + ".json")
    e.require(not path.is_symlink(), "symlink_state", "Refusing symlink resource metadata.")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise e.RunError("invalid_resource_metadata", "Resource metadata is unreadable; preserve it for investigation.") from exc
    e.require(isinstance(value, dict) and value.get("schema") == "delivery-gate-job/v1" and value.get("status") in {"RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"}, "invalid_resource_metadata", "Resource metadata is malformed; preserve it for investigation.")
    return value


def _write_metadata(directory: Path, job: dict) -> None:
    for key in job["lock_keys"]:
        _engine().atomic_json(directory / (key + ".json"), job)


def _checkpoint(root, event, mutate, expected_revision=None) -> dict:
    """Job bookkeeping remains possible after a concurrent lifecycle transition."""
    e = _engine()
    root = Path(root).resolve()
    path = root / ".run.lock"
    e.require(not path.is_symlink(), "symlink_state", "Refusing a symlink run lock.")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        run = e.load(root)
        e.require(expected_revision is None or run["revision"] == expected_revision, "stale_revision", "Reload the run; another writer changed it.")
        mutate(run)
        run["revision"] += 1
        run["updated_at"] = e.now()
        run["history"].append({"revision": run["revision"], "at": run["updated_at"], "event": event, "state": run["state"]})
        e.atomic_json(root / "run.json", run)
        return run
    finally:
        os.close(fd)


def _validate_start(run, task_id, case_id, gate_index, key, resources):
    e = _engine()
    e.assert_plan(run)
    e.require(run["state"] in _ACTIVE, "gate_state", "Gates are not executable in this state.")
    e.require(_resources(run, task_id, case_id, gate_index) == (key, resources), "plan_drift", "Gate ownership changed before resource acquisition.")
    for other_id, owner in run["tasks"].items():
        if owner["status"] == "DISPATCHED":
            held = {r.strip() for r in e.task_plan(run, other_id)["resources"]}
            e.require(other_id != task_id and not held.intersection(resources), "resource_in_use", f"Task {other_id} still holds a gate resource; collect its actual return first.")
    for job in run.get("gate_jobs", {}).values():
        if job["status"] == "RUNNING":
            e.require(job["key"] != key and not set(job["resources"]).intersection(resources), "resource_in_use", "A recorded gate job still holds this gate key or exclusive resource; recover it explicitly if its processes ended.")


def _job(run, context):
    e = _engine()
    job = run.get("gate_jobs", {}).get(context["id"])
    e.require(job and all(job.get(k) == context["identity"].get(k) for k in context["identity"]), "job_identity_drift", "Gate job ownership changed; preserve all resource evidence.")
    return job


def _identity(job):
    return {k: job[k] for k in ("id", "run_root", "host", "runner_pid", "runner_identity", "plan_hash", "key", "task", "case", "resources", "lock_keys")}


def set_process(pid: int) -> None:
    """Checkpoint the actual Popen child before waiting for its result."""
    e = _engine()
    context = _CURRENT.get()
    e.require(context is not None, "missing_gate_job", "Process registration requires an active guarded gate.")
    e.require(type(pid) is int and pid > 0, "invalid_process", "A process ID must be a positive integer.")
    # Preserve the actual Popen PID first. If observation fails, the runner must
    # still reap its child and this durable record must not imply no child exists.
    child = {"pid": pid, "identity": None, "observed_at": e.now()}
    def update(run):
        job = _job(run, context)
        e.require(job["status"] == "RUNNING", "job_state", "The gate job is no longer running.")
        job["child_pid"], job["child_identity"] = pid, None
        job.setdefault("children", []).append(child)
    run = _checkpoint(context["root"], "gate_process_started", update)
    _write_metadata(context["directory"], run["gate_jobs"][context["id"]])
    observed = _observe(pid)
    e.require(not observed["alive"] or observed.get("parent") == os.getpid(), "process_identity_drift", "Only this gate runner's actual child can be registered.")
    def identify(run):
        job = _job(run, context)
        e.require(job["status"] == "RUNNING" and job["children"][-1] == child, "job_identity_drift", "The child checkpoint changed during identity inspection.")
        job["children"][-1]["identity"] = observed["identity"]
        job["child_identity"] = observed["identity"]
    run = _checkpoint(context["root"], "gate_process_identified", identify)
    _write_metadata(context["directory"], run["gate_jobs"][context["id"]])


def _children_dead(job):
    for child in job.get("children", []):
        observed = _observe(child["pid"])
        if observed["alive"] or (observed["identity"] is not None and observed["identity"] != child["identity"]):
            return False
    return True


def _children_compatible(older, current):
    # A crash may interrupt identity enrichment or a later repeat's checkpoint.
    # Earlier metadata may be a prefix, but cannot introduce any other child.
    return len(older) <= len(current) and all(
        before.get("pid") == after.get("pid") and before.get("observed_at") == after.get("observed_at")
        and (before.get("identity") is None or before.get("identity") == after.get("identity"))
        for before, after in zip(older, current))


def _finish(context, result=None, error=None):
    e = _engine()
    current = _job(e.load(context["root"]), context)
    try:
        dead = _children_dead(current)
    except e.RunError:
        dead = False
    receipt = copy.deepcopy(result) if isinstance(result, dict) else None
    if receipt is not None:
        receipt["job_id"] = context["id"]
    def update(run):
        job = _job(run, context)
        job["outcome"] = {"error": str(error) if error else None, "returned": result is not None,
                          "children_confirmed_ended": dead}
        if dead:
            job["status"] = "SUCCEEDED" if error is None and receipt and receipt.get("passed") is True else "FAILED"
            job["finished_at"] = e.now()
        if receipt is not None:
            job["receipt"] = receipt
            job["receipt_hash"] = e.digest(receipt)
            for saved in reversed(run["gates"]):
                if saved == result:
                    saved["job_id"] = context["id"]
                    break
    run = _checkpoint(context["root"], "gate_job_finished" if dead else "gate_job_requires_recovery", update)
    _write_metadata(context["directory"], run["gate_jobs"][context["id"]])
    if not dead and error is None:
        raise e.RunError("gate_process_alive", "A registered gate child is still alive or unobservable; the job remains RUNNING and holds its recorded resources.")
    if isinstance(result, dict):
        result["job_id"] = context["id"]


def guarded(function):
    """Wrap execute_gate; authority and content gates stay in the wrapped engine."""
    if getattr(function, "_delivery_gate_guarded", False):
        return function
    @functools.wraps(function)
    def wrapper(root, **kwargs):
        e = _engine()
        run = e.load(root)
        expected = kwargs.get("expected_revision")
        e.require(expected is None or run["revision"] == expected, "stale_revision", "Reload the run before executing a gate.")
        e.assert_plan(run)
        e.require(run["state"] in _ACTIVE, "gate_state", "Gates are not executable in this state.")
        task_id, case_id, gate_index = kwargs.get("task_id"), kwargs.get("case_id"), kwargs.get("gate_index", 0)
        key, resources = _resources(run, task_id, case_id, gate_index)
        directory = _resource_dir(run)
        keys = _keys(run["root"], key, resources)
        with _locks(directory, keys):
            for lock_key in keys:
                metadata = _metadata(directory, lock_key)
                e.require(not metadata or metadata["status"] != "RUNNING", "resource_recovery_required", "Resource metadata records a RUNNING job; explicitly recover its exact run/job before reuse, including after a host change.")
            runner = _observe(os.getpid())
            e.require(runner["alive"] and runner["identity"], "process_unknown", "The gate runner's start identity must be observable.")
            job = {"schema": "delivery-gate-job/v1", "id": "gate-" + uuid.uuid4().hex,
                   "run_root": run["root"], "plan_hash": run["plan_hash"], "key": key,
                   "task": task_id, "case": case_id, "resources": resources, "lock_keys": keys,
                   "status": "RUNNING", "host": socket.gethostname(), "runner_pid": os.getpid(),
                   "runner_identity": runner["identity"], "child_pid": None, "child_identity": None,
                   "children": [], "started_at": e.now()}
            context = {"root": run["root"], "directory": directory, "id": job["id"], "identity": _identity(job)}
            def start(current):
                _validate_start(current, task_id, case_id, gate_index, key, resources)
                e.require(current["plan_hash"] == job["plan_hash"], "plan_drift", "The gate plan changed before execution.")
                current.setdefault("gate_jobs", {})[job["id"]] = job
            current = _checkpoint(root, "gate_job_started", start, expected)
            token = _CURRENT.set(context)
            try:
                _write_metadata(directory, job)
                if expected is not None:
                    kwargs["expected_revision"] = current["revision"]
                try:
                    result = function(root, **kwargs)
                except BaseException as exc:
                    _finish(context, error=exc)
                    raise
                _finish(context, result=result)
                return result
            finally:
                _CURRENT.reset(token)
    wrapper._delivery_gate_guarded = True
    return wrapper


def recover(root, job_id: str, evidence: str, expected_revision=None) -> dict:
    """Recover only an exactly owned job whose local runner and children ended."""
    e = _engine()
    evidence = e.text(evidence, "Observed recovery evidence")
    run = e.load(root)
    e.require(expected_revision is None or run["revision"] == expected_revision, "stale_revision", "Reload the run before recovery.")
    job = run.get("gate_jobs", {}).get(job_id)
    e.require(job is not None, "unknown_gate_job", "Select a recorded gate job.")
    e.require(job["id"] == job_id, "job_identity_drift", "The recorded gate job identifier changed.")
    e.require(job["host"] == socket.gethostname(), "job_host_mismatch", "Recovery can inspect only jobs recorded on this exact local host.")
    e.require(job["run_root"] == run["root"] and job["lock_keys"] == _keys(run["root"], job["key"], job["resources"]), "job_identity_drift", "The recorded gate/resource ownership has changed.")
    directory = _resource_dir(run)
    context = {"root": run["root"], "directory": directory, "id": job_id, "identity": _identity(job)}
    with _locks(directory, job["lock_keys"]):
        active_metadata = False
        for key in job["lock_keys"]:
            metadata = _metadata(directory, key)
            if metadata is None:
                continue  # The initial checkpoint may have preceded metadata.
            e.require(_identity(metadata) == context["identity"], "job_identity_drift", "Resource metadata belongs to a different job identity; preserve it.")
            e.require(_children_compatible(metadata.get("children", []), job.get("children", [])), "job_identity_drift", "Child process metadata diverged from the durable job checkpoint.")
            active_metadata |= metadata["status"] == "RUNNING"
        e.require(job["status"] == "RUNNING" or active_metadata, "job_state", "This gate job has already finished and released its metadata.")
        processes = [{"pid": job["runner_pid"], "identity": job["runner_identity"]}] + job.get("children", [])
        for process in processes:
            observed = _observe(process["pid"])
            e.require(observed["identity"] is None or observed["identity"] == process["identity"], "process_identity_drift", "A recorded PID now identifies a different process; recovery must not infer its ownership.")
            e.require(not observed["alive"], "gate_process_alive", "The recorded runner or child is still alive; wait for its actual exit. Recovery never kills processes.")
        def interrupted(current):
            owned = _job(current, context)
            e.require(owned.get("children", []) == job.get("children", []), "job_identity_drift", "The process checkpoint changed during recovery.")
            owned["status"] = "INTERRUPTED"
            owned["finished_at"] = e.now()
            owned["recovery"] = {"evidence": evidence, "host": socket.gethostname(), "at": e.now(), "processes_confirmed_ended": True}
        current = _checkpoint(root, "gate_job_recovered", interrupted, expected_revision)
        _write_metadata(directory, current["gate_jobs"][job_id])
    return current
