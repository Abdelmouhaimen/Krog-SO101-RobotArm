"""MuJoCo pose-preview side of the application (no hardware access).

Importing this package does not import ``mujoco``; call
:func:`so101_companion.simulation.gl_backend.select_backend` first, then import the submodules
that need MuJoCo (``mujoco_pose``, ``render``).
"""
