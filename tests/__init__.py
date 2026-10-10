import os

# Human-like reply pacing (H3, R2) waits in wall-clock time; tests that need it enable it explicitly.
os.environ.setdefault('ARTI_HUMAN_PACING', '0')
