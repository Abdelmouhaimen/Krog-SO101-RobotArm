# Future (out of scope): 3D screen registration and pointing

Later the companion may learn the 3D pose of the physical monitor and point toward locations shown in game video. This requires calibration of camera/screen/arm coordinate frames, camera intrinsics, monitor plane geometry, inverse kinematics/collision checks and safety near a player. These capabilities were explicitly deferred and are *not* dependencies of M1–M3's initial high-level behavior tools. Do not sneak IK into M1 because of this future idea.

When revisiting, separately interview the user on monitor position measurement, calibration method, allowed reach, target precision, coordinate-frame ownership, moving screens, and failure states.
