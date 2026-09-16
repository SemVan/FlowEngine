"""Step-skipping test — Phase 3 scaffold.

Protocol (sketched in docs/STEPSKIP.md):
1. Home the axis under test.
2. Step the axis through a parameter ramp (feedrate or load):
   for each setpoint:
       record baseline M114
       command N steps at the setpoint
       wait_idle + M114
       record pressure (PressureSensor.read())
       compare external reference/encoder measurements (NOT M114 coordinates)
3. Emit CSV (setpoint, commanded_steps, actual_steps, pressure, ts) and a
   markdown summary identifying the first setpoint where loss occurred.
"""

from __future__ import annotations


class StepSkipTest:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "Use test_reference in the procedure editor for reference-return error. M114 cannot detect rotor step loss."
        )
