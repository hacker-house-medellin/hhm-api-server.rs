"""ridl -- the Route IDL toolchain.

A route map is a JSON object whose keys are operations and whose values are
typed routes. This package parses it, validates it beyond what JSON Schema can
express, and generates typed clients for every supported language from that one
source, so a route that changes shape breaks the build instead of production.
"""

from .model import SCHEMA_VERSION, RidlError, RouteMap, load_route_map, parse_route_map
from .validate import validate

__all__ = [
    "SCHEMA_VERSION",
    "RidlError",
    "RouteMap",
    "load_route_map",
    "parse_route_map",
    "validate",
]
