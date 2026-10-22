"""Private, locked checkpoints shared by the supervisor and project workers."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(path, nonblocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


class Budget:
    """Reservations bound concurrent coding/research and a separate repair pool."""

    def __init__(self, path, total, repair):
        if not isinstance(total, int) or not isinstance(repair, int) or not 0 < repair < total:
            raise ValueError("Require 0 < repair budget < total budget")
        self.path = Path(path)
        self.limits = {"main": total - repair, "repair": repair}
        with locked(str(self.path) + ".lock"):
            if not self.path.exists():
                save(self.path, {"limits": self.limits, "operations": {}})
            if read(self.path)["limits"] != self.limits:
                raise ValueError("Existing budget limits cannot change on resume")

    def available(self, pool):
        with locked(str(self.path) + ".lock"):
            return self._available(read(self.path), pool)

    def _available(self, state, pool):
        used = sum(item.get("used", item["reserved"]) for item in state["operations"].values()
                   if item["pool"] == pool)
        return max(0, self.limits[pool] - used)

    def reserve(self, identity, pool, allowance):
        with locked(str(self.path) + ".lock"):
            state = read(self.path)
            if identity in state["operations"]:
                previous = state["operations"][identity]
                if previous["pool"] != pool or previous["reserved"] != allowance:
                    raise ValueError("Execution identity was reused with different settings")
                return
            if not isinstance(allowance, int) or allowance <= 0 or allowance > self._available(state, pool):
                raise ValueError("Authorized token allocation exhausted")
            state["operations"][identity] = {"pool": pool, "reserved": allowance}
            save(self.path, state)

    def settle(self, identity, used):
        with locked(str(self.path) + ".lock"):
            state = read(self.path)
            item = state["operations"][identity]
            if not isinstance(used, int) or used < 0:
                raise ValueError("Invalid token usage")
            if "used" in item and item["used"] != used:
                raise ValueError("Conflicting usage receipt")
            item["used"] = used
            save(self.path, state)
            if used > item["reserved"]:
                raise ValueError("Execution exceeded its reserved token allowance")


class WorkQueue:
    """Immutable task requests and durable execution/delivery identities.

    A claim is never automatically replayed. Reconcile its saved execution before
    recording delivery. A failed execution needs an explicitly new task identity.
    """

    def __init__(self, path, binding):
        self.path = Path(path)
        with locked(str(self.path) + ".lock"):
            if not self.path.exists():
                save(self.path, {"binding": binding, "tasks": {}})
            if read(self.path)["binding"] != binding:
                raise ValueError("Queue source/protocol binding changed")

    def add(self, identity, request):
        with locked(str(self.path) + ".lock"):
            state = read(self.path)
            previous = state["tasks"].get(identity)
            if previous and previous["request"] != request:
                raise ValueError("Task identity was reused with a different request")
            state["tasks"].setdefault(identity, {"request": request, "status": "pending"})
            save(self.path, state)

    def claim(self, identity):
        with locked(str(self.path) + ".lock"):
            state = read(self.path)
            task = state["tasks"][identity]
            task.setdefault("execution", identity + ":1")
            if task["status"] == "pending":
                task["status"] = "admitted"
            save(self.path, state)
            return task["execution"]

    def record(self, identity, execution, receipt):
        with locked(str(self.path) + ".lock"):
            state = read(self.path)
            task = state["tasks"][identity]
            if task.get("execution") != execution:
                raise ValueError("Delivery must cite the admitted execution")
            if "receipt" in task and task["receipt"] != receipt:
                raise ValueError("Conflicting delivery receipt")
            task.update(status="complete", receipt=receipt)
            save(self.path, state)

    def frontier(self):
        with locked(str(self.path) + ".lock"):
            return [{"id": key, **value} for key, value in read(self.path)["tasks"].items()
                    if value["status"] != "complete"]

    def result(self, identity):
        with locked(str(self.path) + ".lock"):
            return read(self.path)["tasks"][identity].get("receipt")

    def completed(self):
        with locked(str(self.path) + ".lock"):
            return [key for key, value in read(self.path)["tasks"].items() if value["status"] == "complete"]
