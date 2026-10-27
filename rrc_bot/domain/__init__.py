"""Pure domain engine: scoring rules and calculations.

Nothing in this package touches Discord, the database, the clock or the network.
Every function is deterministic, which is what makes recomputation safe.
"""
