import subprocess
import json

class ResticUI:
    def __init__(self):
        pass

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
        Return a list of lines from `restic ls` for the given snapshot.

        This is used by the API endpoint to build a directory tree. If the
        repository is very large, this call can still be expensive, but the
        work of turning it into HTML is moved to the client and we cap the
        number of entries processed in the Flask app.
        """
        try:
            result = subprocess.run(
                ["restic", "ls", snapshot_id],
                capture_output=True,
                text=True,
                check=True
            )
            # Return list of lines as contents
            return result.stdout.strip().splitlines()
        except subprocess.CalledProcessError:
            return []
