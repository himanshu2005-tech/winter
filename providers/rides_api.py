# -*- coding: utf-8 -*-
"""
Ride hailing for VANTA, by deep link rather than by API.

Uber's ride-request API needs a registered app plus rider OAuth, which means
storing a credential that can book rides on Himanshu's account. Uber's own
guidance points new integrations at deep links to m.uber.com instead, and that
is the better shape here for a reason beyond convenience:

VANTA prepares the ride, Himanshu confirms it.

A ride costs money and puts a car on a road. The Uber screen he taps is a real
confirmation by a person who can see the price and the pickup point -- which is
worth more than any approval gate VANTA could offer, because it is shown in
the app that will charge him. Nothing here books anything. It fills in the form
and opens it.

Free: Nominatim for geocoding (already used for weather and travel time, so the
two cannot disagree about where a place is), and the deep link itself. A
client_id from Uber's free developer registration unlocks product selection;
without one the link still opens with the trip filled in.
"""

import json
import os
from urllib.parse import quote, urlencode

# The universal link. The older widget was deprecated in 2018 and the docs send
# new work here.
_LOOKING = "https://m.uber.com/looking"
_UL = "https://m.uber.com/ul/"


def _place(lat, lon, name="", address=""):
    """One end of a trip, in the object shape the deep link expects."""
    payload = {"latitude": float(lat), "longitude": float(lon)}
    if name:
        payload["addressLine1"] = str(name)[:80]
    if address:
        payload["addressLine2"] = str(address)[:120]
    return payload


def build_uber_link(pickup, dropoff, client_id=""):
    """A link that opens Uber with the trip already filled in.

    pickup and dropoff are dicts from _place. Returns the URL.
    """
    client_id = client_id or os.getenv("UBER_CLIENT_ID", "").strip()

    if client_id:
        params = {
            "client_id": client_id,
            "pickup": json.dumps(pickup, separators=(",", ":")),
            "drop[0]": json.dumps(dropoff, separators=(",", ":")),
        }
        return f"{_LOOKING}?{urlencode(params, quote_via=quote)}"

    # No client_id registered. The older parameter form still opens the app
    # with the trip set, and asking him to register a developer app before he
    # can be driven anywhere would be a silly place to stop.
    params = {
        "action": "setPickup",
        "pickup[latitude]": pickup["latitude"],
        "pickup[longitude]": pickup["longitude"],
        "dropoff[latitude]": dropoff["latitude"],
        "dropoff[longitude]": dropoff["longitude"],
    }
    if pickup.get("addressLine1"):
        params["pickup[nickname]"] = pickup["addressLine1"]
    if dropoff.get("addressLine1"):
        params["dropoff[nickname]"] = dropoff["addressLine1"]
    if dropoff.get("addressLine2"):
        params["dropoff[formatted_address]"] = dropoff["addressLine2"]
    return f"{_UL}?{urlencode(params, quote_via=quote)}"


def prepare_ride(destination, pickup=""):
    """Work out both ends and return (url, description) or (None, error).

    destination is text he said. pickup is text too, or empty to mean "where he
    is now", which ambient already knows from the same coordinates the weather
    came from.
    """
    destination = str(destination or "").strip()
    if not destination:
        return None, ("[Error] BOOK_RIDE needs a destination. Ask him where he "
                      "is going rather than guessing.")

    from providers.maps_api import get_coordinates

    dest_lat, dest_lon = get_coordinates(destination)
    if not dest_lat:
        return None, (f"[Error] I could not find a place called "
                      f"'{destination}'. Ask him to name it differently.")

    pickup_text = str(pickup or "").strip()
    if pickup_text:
        from_lat, from_lon = get_coordinates(pickup_text)
        if not from_lat:
            return None, (f"[Error] I could not find the pickup point "
                          f"'{pickup_text}'.")
        origin_label = pickup_text
    else:
        try:
            import ambient
            place = ambient.location()
        except Exception:
            place = None
        if not place:
            return None, ("[Error] I do not know where he is, so I cannot set "
                          "a pickup point. Ask him where to collect him.")
        from_lat, from_lon = place["lat"], place["lon"]
        origin_label = place.get("city") or "Current location"

    url = build_uber_link(
        _place(from_lat, from_lon, origin_label),
        _place(dest_lat, dest_lon, destination, destination),
    )
    described = (f"Uber ready: {origin_label} -> {destination}. "
                 f"Opening the app with the trip filled in; he confirms the "
                 f"price and taps Request himself. Nothing has been booked.")
    return url, described
