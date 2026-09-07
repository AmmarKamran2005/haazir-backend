"""Every model, imported so that `Base.metadata` is complete.

Alembic's autogenerate and the test fixtures both read `Base.metadata`. A model module that is
never imported is invisible to both, which shows up as a table that exists in the code and not
in the database.
"""

from .auth import AppUser, DeviceToken, GroupToken, MagicLink, RefreshToken
from .dish import Dish, DishAvailability, DishTimeQuality, VenueDish, VenueDishPrice
from .live import LiveState, Observation, OccupancyPrior, SourceCalibration
from .partner import Attribution, Offer
from .social import (
    FactVerification,
    GroupConstraint,
    GroupMember,
    GroupSession,
    GroupSolution,
    Visit,
)
from .trust import RegulatoryEvent, RegulatoryReply, ReviewSample, TrustScore
from .venue import Area, City, Venue, VenueSource

__all__ = [
    "AppUser",
    "Area",
    "Attribution",
    "City",
    "DeviceToken",
    "Dish",
    "DishAvailability",
    "DishTimeQuality",
    "FactVerification",
    "GroupConstraint",
    "GroupMember",
    "GroupSession",
    "GroupSolution",
    "GroupToken",
    "LiveState",
    "MagicLink",
    "Observation",
    "OccupancyPrior",
    "Offer",
    "RefreshToken",
    "RegulatoryEvent",
    "RegulatoryReply",
    "ReviewSample",
    "SourceCalibration",
    "TrustScore",
    "Venue",
    "VenueDish",
    "VenueDishPrice",
    "VenueSource",
    "Visit",
]
