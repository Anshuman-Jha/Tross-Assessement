"""Reading Voyager's *normalised* response format.

Because we send ``accept: application/vnd.linkedin.normalized+json+2.1``,
LinkedIn flattens the object graph: every entity is hoisted into a top-level
``included[]`` array keyed by ``entityUrn``, and the places that used to hold a
nested object instead hold a URN *reference*, marked by a ``*`` key prefix::

    {
      "data": { "*elements": ["urn:li:fsd_profile:ABC"] },
      "included": [
        { "entityUrn": "urn:li:fsd_profile:ABC", "firstName": "Ada", ... }
      ]
    }

So ``"*elements"`` means "the value is a URN (or list of URNs); look it up in
``included``". This module builds that index and dereferences on demand.

Resolution is lazy and depth-bounded on purpose. The graph contains genuine
cycles (a profile references a position which references the profile), so a
naive eager expansion either loops forever or explodes in memory.
"""

from __future__ import annotations

from typing import Any

from app.observability.logging import get_logger

logger = get_logger(__name__)

MAX_DEPTH = 12


class EntityIndex:
    """An index over ``included[]`` supporting URN dereferencing."""

    def __init__(self, payload: object) -> None:
        self._by_urn: dict[str, dict[str, Any]] = {}
        self._by_type: dict[str, list[dict[str, Any]]] = {}
        self._data: dict[str, Any] = {}

        if isinstance(payload, dict):
            data = payload.get("data")
            self._data = data if isinstance(data, dict) else {}
            self._ingest(payload.get("included"))
            # Some legacy REST responses inline entities under `elements`
            # without an `included` array at all.
            if not self._by_urn:
                self._ingest(payload.get("elements"))

    def _ingest(self, included: object) -> None:
        if not isinstance(included, list):
            return
        for entity in included:
            if not isinstance(entity, dict):
                continue
            urn = entity.get("entityUrn")
            if isinstance(urn, str) and urn:
                self._by_urn[urn] = entity
            type_name = entity.get("$type")
            if isinstance(type_name, str):
                self._by_type.setdefault(type_name, []).append(entity)

    # ------------------------------------------------------------- accessors

    def __len__(self) -> int:
        return len(self._by_urn)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def entities(self) -> list[dict[str, Any]]:
        return list(self._by_urn.values())

    def get(self, urn: str | None) -> dict[str, Any] | None:
        if not urn or not isinstance(urn, str):
            return None
        return self._by_urn.get(urn)

    def by_type(self, *suffixes: str) -> list[dict[str, Any]]:
        """Entities whose ``$type`` ends with any of the given suffixes.

        Matching on the suffix rather than the full name keeps this working
        across LinkedIn's ``voyager.identity.profile`` and
        ``voyager.dash.identity.profile`` namespaces, which coexist.
        """
        out: list[dict[str, Any]] = []
        for type_name, entities in self._by_type.items():
            if any(type_name.endswith(s) or type_name == s for s in suffixes):
                out.extend(entities)
        return out

    def first_of_type(self, *suffixes: str) -> dict[str, Any] | None:
        found = self.by_type(*suffixes)
        return found[0] if found else None

    # ---------------------------------------------------------- dereferencing

    def deref(self, value: object, depth: int = 0) -> Any:
        """Follow a URN, or a list of URNs, into the index.

        Returns the entity dict (or list of them). Unresolvable URNs come back
        as-is so callers can still see what was referenced.
        """
        if depth > MAX_DEPTH:
            return value
        if isinstance(value, str):
            found = self.get(value)
            return found if found is not None else value
        if isinstance(value, list):
            return [self.deref(v, depth + 1) for v in value]
        return value

    def resolve(self, node: object, depth: int = 0, _seen: frozenset[str] | None = None) -> Any:
        """Recursively expand ``*``-prefixed references within ``node``.

        The ``*`` prefix is stripped from resolved keys, so ``"*elements"``
        becomes ``"elements"`` holding real objects. Cycles are cut by tracking
        the URNs already on the current path.
        """
        seen = _seen or frozenset()
        if depth > MAX_DEPTH:
            return node

        if isinstance(node, list):
            return [self.resolve(item, depth + 1, seen) for item in node]

        if not isinstance(node, dict):
            return node

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("*"):
                plain = key[1:]
                resolved = self._resolve_reference(value, depth, seen)
                # Keep the raw URN alongside, some callers need the identifier.
                out[plain] = resolved
                out.setdefault(f"{plain}Urn", value)
            else:
                out[key] = self.resolve(value, depth + 1, seen)
        return out

    def _resolve_reference(self, value: object, depth: int, seen: frozenset[str]) -> Any:
        if isinstance(value, list):
            return [self._resolve_reference(v, depth, seen) for v in value]
        if not isinstance(value, str):
            return self.resolve(value, depth + 1, seen)
        if value in seen:
            # Cycle: hand back the bare URN rather than recursing.
            return value
        entity = self.get(value)
        if entity is None:
            return value
        return self.resolve(entity, depth + 1, seen | {value})


def find_all(node: object, predicate: Any, *, max_depth: int = 14) -> list[dict[str, Any]]:
    """Depth-first search for every dict in ``node`` satisfying ``predicate``.

    A pragmatic escape hatch: Voyager occasionally relocates a field between
    releases, and searching for its shape is more durable than pinning a path.
    """
    found: list[dict[str, Any]] = []

    def walk(current: object, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(current, dict):
            try:
                if predicate(current):
                    found.append(current)
            except Exception as exc:
                # Predicates are caller-supplied and run against arbitrary
                # upstream shapes; one raising must not abort the whole search.
                logger.debug("find_all.predicate_raised", error=str(exc))
            for value in current.values():
                walk(value, depth + 1)
        elif isinstance(current, list):
            for item in current:
                walk(item, depth + 1)

    walk(node, 0)
    return found


def first(node: object, predicate: Any, *, max_depth: int = 14) -> dict[str, Any] | None:
    results = find_all(node, predicate, max_depth=max_depth)
    return results[0] if results else None


def dig(node: object, *path: str, default: Any = None) -> Any:
    """Safe nested lookup: ``dig(d, "a", "b", "c")``.

    Traverses dicts by key and lists by taking the first element, which matches
    how Voyager wraps single values in one-element arrays.
    """
    current: Any = node
    for key in path:
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def text_of(node: object) -> str | None:
    """Extract a display string from Voyager's several text wrappers.

    Text arrives as a bare string, ``{"text": "..."}``, or the attributed form
    ``{"text": {"text": "..."}}`` used by the GraphQL tier.
    """
    if isinstance(node, str):
        stripped = node.strip()
        return stripped or None
    if isinstance(node, dict):
        for key in ("text", "textDirection", "accessibilityText"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = text_of(value)
                if nested:
                    return nested
    if isinstance(node, list) and node:
        return text_of(node[0])
    return None
