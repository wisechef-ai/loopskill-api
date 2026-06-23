"""Thin CLI wrapper. See incident_reporter.run_cli for behavior."""
import sys
from incident_reporter import run_cli

if __name__ == "__main__":
    sys.exit(run_cli())
