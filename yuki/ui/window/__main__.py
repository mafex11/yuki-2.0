"""``pythonw -m yuki.ui.window``: the Yuki window, as its own short-lived process (see yuki.ui.window.app)."""

import sys

from yuki.ui.window.app import main

sys.exit(main())
