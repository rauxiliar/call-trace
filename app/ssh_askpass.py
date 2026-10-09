#!/usr/bin/env python3
"""Print the SSH password from the environment. The password is not on the command line."""

import os
import sys

try:
    sys.stdout.write(os.environ.get("CALLTRACE_SSH_PASSWORD", "") + "\n")
    sys.stdout.flush()
except BrokenPipeError:
    pass
try:
    sys.stdout.close()
except BrokenPipeError:
    pass
sys.exit(0)
