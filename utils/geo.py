from math import asin, cos, radians, sin, sqrt


def haversine_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Return the great-circle distance between two points in kilometers."""
    earth_radius_km = 6371.0088
    latitude_delta = radians(latitude_b - latitude_a)
    longitude_delta = radians(longitude_b - longitude_a)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(radians(latitude_a)) * cos(radians(latitude_b)) * sin(longitude_delta / 2) ** 2
    )
    return 2 * earth_radius_km * asin(sqrt(min(1.0, haversine)))


def sort_orders_by_distance(orders, latitude: float, longitude: float):
    """Sort geocoded orders nearest-first; keep orders without coordinates at the end."""
    return sorted(
        orders,
        key=lambda order: (
            order.latitude is None or order.longitude is None,
            haversine_km(latitude, longitude, order.latitude, order.longitude)
            if order.latitude is not None and order.longitude is not None
            else float("inf"),
        ),
    )