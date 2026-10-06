"""Kindle Scribe as a graphics tablet.

Turns a jailbroken Kindle Scribe into a drawing surface for a host machine:
the host owns the canvas, the canvas is streamed to the Kindle over SSH +
FBInk, and stylus events from the Kindle are captured, normalized and injected
back into the host (or drawn directly on the canvas).
"""

__version__ = "0.1.0"