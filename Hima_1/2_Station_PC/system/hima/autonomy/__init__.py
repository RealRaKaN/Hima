"""Autonomy, part 1: the flight plan made on the Jetson before take-off.

reserve  - the reserve boundary, the ArduPilot fence inside it, the grid of cells, the launch points
history  - what past flights left: their paths, herd sightings, poaching incidents
planner  - scores every cell and builds today's route within the energy budget of the VTOL quadplane
export   - the route and the fence as ArduPilot mission files, plus a picture of the plan

The in-flight behaviour (follow a herd, follow a vehicle near a herd, return home when the Jetson
fails) is the next part and is not here yet.
"""
