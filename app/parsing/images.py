"""Reassembling LinkedIn image URLs.

LinkedIn never returns a ready-to-use image URL. It returns a ``vectorImage``:
a CDN root plus a list of artifacts, one per rendered width, each contributing
the path segment for its own size::

    {
      "rootUrl": "https://media.licdn.com/dms/image/D4E03AQH.../profile-displayphoto-shrink_",
      "artifacts": [
        {"width": 100, "height": 100, "fileIdentifyingUrlPathSegment": "100_100/0/16..."},
        {"width": 400, "height": 400, "fileIdentifyingUrlPathSegment": "400_400/0/16..."}
      ]
    }

The usable URL is ``rootUrl + fileIdentifyingUrlPathSegment``. Every size is
kept so a consumer can pick, with the largest surfaced as the default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImageArtifact(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None
    expires_at: int | None = Field(
        default=None,
        description="LinkedIn CDN URLs are signed and expire, typically within weeks.",
    )


class Image(BaseModel):
    url: str = Field(description="Highest-resolution variant available.")
    width: int | None = None
    height: int | None = None
    artifacts: list[ImageArtifact] = Field(default_factory=list)


def parse_vector_image(raw: object) -> Image | None:
    """Build an :class:`Image` from a ``vectorImage`` object.

    Tolerates the several shapes Voyager wraps these in across tiers.
    """
    node = _unwrap(raw)
    if not isinstance(node, dict):
        return None

    root = node.get("rootUrl")
    artifacts_raw = node.get("artifacts")
    if not isinstance(root, str) or not isinstance(artifacts_raw, list):
        return None

    artifacts: list[ImageArtifact] = []
    for art in artifacts_raw:
        if not isinstance(art, dict):
            continue
        segment = art.get("fileIdentifyingUrlPathSegment")
        if not isinstance(segment, str) or not segment:
            continue
        artifacts.append(
            ImageArtifact(
                url=f"{root}{segment}",
                width=_as_int(art.get("width")),
                height=_as_int(art.get("height")),
                expires_at=_as_int(art.get("expiresAt")),
            )
        )

    if not artifacts:
        return None

    largest = max(artifacts, key=lambda a: (a.width or 0) * (a.height or 0))
    return Image(
        url=largest.url,
        width=largest.width,
        height=largest.height,
        artifacts=sorted(artifacts, key=lambda a: a.width or 0),
    )


def _unwrap(raw: object) -> object:
    """Peel the wrappers Voyager puts around a vectorImage.

    Seen in the wild: the bare object, ``{"vectorImage": {...}}``,
    ``{"com.linkedin.common.VectorImage": {...}}``, and the GraphQL
    ``{"attributes": [{"detailData": {"vectorImage": {...}}}]}`` form.
    """
    seen = 0
    node = raw
    while seen < 6:
        seen += 1
        if not isinstance(node, dict):
            return node
        if "rootUrl" in node and "artifacts" in node:
            return node
        for key in (
            "vectorImage",
            "com.linkedin.common.VectorImage",
            "image",
            "detailData",
            "displayImageReference",
            "profilePicture",
            "backgroundImage",
        ):
            if key in node:
                node = node[key]
                break
        else:
            attrs = node.get("attributes")
            if isinstance(attrs, list) and attrs:
                node = attrs[0]
                continue
            return None
    return None


def parse_image_from_component(node: object) -> Image | None:
    """Pull an image out of a GraphQL entity component's ``image`` field."""
    if not isinstance(node, dict):
        return None
    for key in ("image", "logo", "picture", "icon"):
        if key in node:
            found = parse_vector_image(node[key])
            if found:
                return found
    return parse_vector_image(node)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None
