"""Retailer adapter registry."""

from __future__ import annotations

from ..http_client import PoliteSession
from .amazon_in import AmazonIN
from .base import Offer, Source, parse_inr
from .firstcry import FirstCry
from .flipkart import Flipkart
from .hamleys import Hamleys
from .lego_in import LegoIN
from .shopify import JaimanToys, MyBrickHouse, ShopifyStore, Toycra

SOURCE_CLASSES = {
    AmazonIN.name: AmazonIN,
    Flipkart.name: Flipkart,
    FirstCry.name: FirstCry,
    Hamleys.name: Hamleys,
    JaimanToys.name: JaimanToys,
    MyBrickHouse.name: MyBrickHouse,
    Toycra.name: Toycra,
    LegoIN.name: LegoIN,
}

#: Sources that can actually report a rupee price. LEGO.com is excluded — it
#: renders prices client-side only (see sources/lego_in.py).
PRICE_SOURCES = ("amazon_in", "flipkart", "firstcry",
                 "hamleys", "jaimantoys", "mybrickhouse", "toycra")

#: The storefront the browsable catalogue is built from.
CATALOG_SOURCE = MyBrickHouse.name

#: What a live search fans out to by default.
SEARCH_SOURCES = PRICE_SOURCES

RETAILER_LABELS = {name: cls.label for name, cls in SOURCE_CLASSES.items()}


def build_sources(session: PoliteSession,
                  only: list[str] | None = None) -> list[Source]:
    names = only or list(SOURCE_CLASSES)
    out = []
    for name in names:
        cls = SOURCE_CLASSES.get(name)
        if cls is None:
            raise KeyError(f"unknown source '{name}'. "
                           f"Known: {', '.join(SOURCE_CLASSES)}")
        out.append(cls(session))
    return out


__all__ = [
    "AmazonIN", "FirstCry", "Flipkart", "Hamleys", "JaimanToys", "LegoIN",
    "MyBrickHouse", "ShopifyStore", "Toycra",
    "Offer", "Source", "parse_inr",
    "SOURCE_CLASSES", "PRICE_SOURCES", "SEARCH_SOURCES", "RETAILER_LABELS",
    "CATALOG_SOURCE", "build_sources",
]
