"""Run packaged source with the private runtime, ignoring user Python settings."""
import runpy
import sys

sys.path.insert(0, "/usr/lib/capsa/app/federation")
runpy.run_module("capsa_federation", run_name="__main__", alter_sys=True)
