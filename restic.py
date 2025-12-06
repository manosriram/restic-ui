import subprocess
import json
import time

class ResticUI:
    def __init__(self):
        # simple in-process cache: {snapshot_id: (timestamp, entries)}
        # use getattr so multiple ResticUI() creations in same process share cache
        self._ls_cache = getattr(self, "_ls_cache", {})
        self._ls_ttl = 300  # seconds; adjust as needed

    def get_snapshots(self):
        try:
            result = subprocess.run(
                ["restic", "snapshots", "--json"],
                capture_output=True,
                text=True,
                check=True
            )
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    def get_snapshot_contents(self, snapshot_id):
        """
        Return a list of JSON entries from `restic ls --json` for the given snapshot.

        Each entry is a dict that includes at least:
          - "path": the full path within the snapshot
          - "type": "file" or "dir"

        Results are cached in memory per snapshot for a short time so that
        every dropdown click does not re-run `restic ls`.
        """
        now = time.time()
        cached = self._ls_cache.get(snapshot_id)
        if cached:
            ts, entries = cached
            if now - ts < self._ls_ttl:
                return entries

        try:
            result = subprocess.run(
                ["restic", "ls", "--json", snapshot_id],
                capture_output=True,
                text=True,
                check=True
            )
            entries = []
            # restic ls --json outputs one JSON object per line
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Some versions wrap the node under "node"
                node = obj.get("node", obj)
                path = node.get("path")
                if not path:
                    continue
                entry_type = node.get("type") or node.get("node_type") or ""
                entries.append({
                    "path": path.lstrip("/"),
                    "type": entry_type,
                })
            # store in cache
            self._ls_cache[snapshot_id] = (now, entries)
            return entries
        except subprocess.CalledProcessError:
            return []
