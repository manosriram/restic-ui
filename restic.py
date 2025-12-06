import subprocess
import json

class ResticUI:
    def __init__(self):
        pass

    def get_snapshots(self):
        """Use local restic CLI to get snapshots and return as dict"""
        try:
            result = subprocess.run(
                ["restic", "snapshots", "--json"],
                capture_output=True,
                text=True,
                check=True
            )
            snapshots = json.loads(result.stdout)
            return snapshots
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            # You might want to handle/log the error properly in real use
            return {}
