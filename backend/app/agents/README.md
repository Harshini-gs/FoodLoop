Agent nodes live here. Implement per docs/SPECS.md section 4.

permissions.py is the file to write first: it maps each node name to the
frozenset of tool names it may call, and a decorator enforces it at call time.
Nothing anywhere is granted `record_handover`.
