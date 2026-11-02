#!/usr/bin/env python3
"""Chat with one Grok Bot. Inside Herdr, the pane shows up in the agents list.

The bot list is an overview: who needs you, who is running, and who has a new reply.
"""

import os
import sys

# A symlink in ~/bin resolves to this file, so the gbottui package beside it is found.
# Copying this file on its own is not enough: the package has to sit next to it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from gbottui.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
