"""Identity fields — name, headline, location, about, images, counts.

Unlike the list sections, basics have to be read from whichever tier answered,
and the three tiers disagree about field names:

* the **REST** tier returns a flat ``Profile`` entity with ``firstName``,
  ``locationName``, ``summary``;
* the **GraphQL** tier returns the same person as a ``topCard`` component tree
  plus a dash ``Profile`` entity that uses ``geoLocation`` and different image
  wrappers;
* the **HTML** tier inlines whichever of those two the page happened to use.

So this module looks the entity up by ``$type`` suffix, then reads each field
through a list of candidate keys. That is deliberately more forgiving than
pinning one path: LinkedIn renames these fields between releases, and a missing
name is a far worse failure than a slightly loose lookup.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.profile import Basics, ContactInfo, Location
from app.parsing.collection import EntityIndex, dig, text_of
from app.parsing.components import FlatEntity
from app.parsing.images import parse_vector_image

_PROFILE_TYPES = ("identity.profile.Profile", "identity.shared.MiniProfile", ".Profile")

_FIRST_NAME_KEYS = ("firstName",)
_LAST_NAME_KEYS = ("lastName",)
_HEADLINE_KEYS = ("headline", "occupation")
_ABOUT_KEYS = ("summary", "about")
_LOCATION_KEYS = ("locationName", "geoLocationName", "defaultLocalizedNameWithoutCountryName")
_COUNTRY_KEYS = ("geoCountryName", "countryName")
_INDUSTRY_KEYS = ("industryName", "industry")

#: "1,234 followers" / "500+ connections"
_COUNT_RE = re.compile(r"([\d,]+)\s*\+?\s*(follower|connection)", re.IGNORECASE)


def parse_basics(
    payload: object,
    *,
    public_id: str | None = None,
    top_card: list[FlatEntity] | None = None,
) -> Basics:
    """Assemble :class:`Basics` from a normalised payload, whichever tier it came from."""
    index = payload if isinstance(payload, EntityIndex) else EntityIndex(payload)
    entity = _find_profile_entity(index)

    basics = Basics(public_identifier=public_id)

    if entity:
        basics.first_name = _first_str(entity, _FIRST_NAME_KEYS)
        basics.last_name = _first_str(entity, _LAST_NAME_KEYS)
        basics.headline = _first_str(entity, _HEADLINE_KEYS)
        basics.about = _first_str(entity, _ABOUT_KEYS)
        basics.industry = _first_str(entity, _INDUSTRY_KEYS) or _industry_from_urn(entity, index)
        basics.public_identifier = (
            _first_str(entity, ("publicIdentifier",)) or basics.public_identifier
        )
        basics.location = _location(entity)
        basics.profile_picture = _image(entity, ("profilePicture", "picture"))
        basics.background_image = _image(entity, ("backgroundImage", "backgroundPicture"))
        basics.is_premium = _first_bool(entity, ("premium", "premiumSubscriber"))
        basics.is_influencer = _first_bool(entity, ("influencer",))
        basics.followers = _first_int(entity, ("followerCount", "followersCount"))
        basics.connections = _first_int(entity, ("connectionsCount", "connections"))

    # The GraphQL top card carries headline/location as rendered strings and is
    # often the only place the About text appears.
    if top_card:
        _merge_top_card(basics, top_card)

    if basics.full_name is None:
        parts = [p for p in (basics.first_name, basics.last_name) if p]
        basics.full_name = " ".join(parts) if parts else None

    _read_counts_from_anywhere(basics, index)
    return basics


def _find_profile_entity(index: EntityIndex) -> dict[str, Any] | None:
    """The person's own Profile entity.

    A response contains many profile entities (recommenders, colleagues), so
    the one carrying a headline or summary is preferred over the first match.
    """
    candidates = index.by_type(*_PROFILE_TYPES)
    if not candidates:
        return None
    for entity in candidates:
        if any(entity.get(k) for k in (*_HEADLINE_KEYS, *_ABOUT_KEYS)):
            return entity
    return candidates[0]


def _merge_top_card(basics: Basics, top_card: list[FlatEntity]) -> None:
    """Fill gaps from the rendered top-card components."""
    for entity in top_card:
        if not basics.full_name and entity.title:
            basics.full_name = entity.title
        if not basics.headline and entity.subtitle:
            basics.headline = entity.subtitle
        if not basics.location.full and entity.caption:
            basics.location.full = entity.caption
        if not basics.about and entity.description:
            basics.about = entity.description
        if not basics.profile_picture and entity.image:
            basics.profile_picture = entity.image


def _location(entity: dict[str, Any]) -> Location:
    full = _first_str(entity, _LOCATION_KEYS)
    country = _first_str(entity, _COUNTRY_KEYS)
    # The dash schema nests the display name a level down.
    if not full:
        full = text_of(dig(entity, "geoLocation", "geo", "defaultLocalizedName"))
    city = None
    if full and "," in full:
        city = full.split(",")[0].strip() or None
    return Location(full=full, city=city, country=country)


def _image(entity: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in entity:
            found = parse_vector_image(entity[key])
            if found:
                return found
            # The dash schema wraps it one level deeper.
            found = parse_vector_image(dig(entity, key, "displayImageReference"))
            if found:
                return found
    return None


def _industry_from_urn(entity: dict[str, Any], index: EntityIndex) -> str | None:
    """Industry often arrives as a URN pointing at a separate entity."""
    for key in ("industryUrn", "*industry", "industry"):
        value = entity.get(key)
        if isinstance(value, str) and value.startswith("urn:li:"):
            referenced = index.get(value)
            if referenced:
                return text_of(referenced.get("name")) or referenced.get("localizedName")
    return None


def _read_counts_from_anywhere(basics: Basics, index: EntityIndex) -> None:
    """Recover follower/connection counts from rendered strings.

    These live in a different entity depending on tier, so rather than pinning
    a path we scan for the rendered "1,234 followers" text.
    """
    if basics.followers is not None and basics.connections is not None:
        return
    for entity in index.entities:
        for value in entity.values():
            if not isinstance(value, str):
                continue
            for raw, kind in _COUNT_RE.findall(value):
                try:
                    number = int(raw.replace(",", ""))
                except ValueError:  # pragma: no cover - regex guarantees digits
                    continue
                if kind.lower() == "follower" and basics.followers is None:
                    basics.followers = number
                elif kind.lower() == "connection" and basics.connections is None:
                    basics.connections = number


def parse_contact_info(payload: object) -> ContactInfo:
    """Read the ``profileContactInfo`` response.

    Only ever populated for connections who chose to share these details; for
    most profiles every list here is legitimately empty.
    """
    contact = ContactInfo()
    index = payload if isinstance(payload, EntityIndex) else EntityIndex(payload)

    source: dict[str, Any] | None = index.first_of_type("ProfileContactInfo")
    if source is None:
        source = index.data or None
    if not isinstance(source, dict):
        return contact

    for site in _as_list(source.get("websites")):
        url = site.get("url") if isinstance(site, dict) else site
        if isinstance(url, str) and url:
            contact.websites.append(url)

    for handle in _as_list(source.get("twitterHandles")):
        name = handle.get("name") if isinstance(handle, dict) else handle
        if isinstance(name, str) and name:
            contact.twitter.append(name)

    for phone in _as_list(source.get("phoneNumbers")):
        number = dig(phone, "number") if isinstance(phone, dict) else phone
        if isinstance(number, str) and number:
            contact.phones.append(number)

    email = source.get("emailAddress")
    if isinstance(email, dict):
        email = email.get("emailAddress")
    if isinstance(email, str) and email:
        contact.emails.append(email)

    birthday = source.get("birthDateOn") or source.get("birthdayVisibilitySetting")
    if isinstance(birthday, dict):
        month, day = birthday.get("month"), birthday.get("day")
        if month and day:
            contact.birthday = f"{month:02d}-{day:02d}" if isinstance(month, int) else None

    address = source.get("address")
    if isinstance(address, str) and address.strip():
        contact.address = address.strip()

    return contact


# ------------------------------------------------------------------- helpers


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if value else []


def _first_str(entity: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entity.get(key)
        text = text_of(value)
        if text:
            return text
    return None


def _first_int(entity: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = entity.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _first_bool(entity: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = entity.get(key)
        if isinstance(value, bool):
            return value
    return None
