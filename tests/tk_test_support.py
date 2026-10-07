"""Keep destroyed test interpreters out of worker-side cyclic collection."""

import gc
import tkinter as tk


def create_test_root():
    gc.collect()
    root = tk.Tk()
    closed = False

    def destroy():
        nonlocal closed
        if closed:
            return
        widgets, pending = [], [root]
        while pending:
            widget = pending.pop()
            widgets.append(widget)
            pending.extend(widget.children.values())
        tk.Tk.destroy(root)
        closed = True
        # Mock exception tracebacks may retain widgets after normal Tk destruction.
        for widget in widgets:
            widget.tk = None
        gc.collect()

    root.destroy = destroy
    return root
