"""Retailer adapter registry."""

from __future__ import annotations

from ..http_client import PoliteSession
from .amazon_in import AmazonIN
from .buyhatke import BuyHatke
from .base import Offer, Source, parse_inr
from .firstcry import FirstCry
from .flipkart import Flipkart
from .hamleys import Hamleys
from .lego_in import LegoIN
from .shopify import JaimanToys, MyBrickHouse, ShopifyStore, Toycra

SOURCE_CLASSES = {
    AmazonIN.name: AmazonIN,
    BuyHatke.name: BuyHatke,
    Flipkart.name: Flipkart,
    FirstCry.name: FirstCry,
    Hamleys.name: Hamleys,
    JaimanToys.name: JaimanToys,
    MyBrickHouse.name: MyBrickHouse,
    Toycra.name: Toycra,
    LegoIN.name: LegoIN,
}

#: BuyHatke is the only active price source: it already watches Amazon,
#: Flipkart, Hamleys and others itself, for free, and hands back a labelled
#: cross-retailer comparison in one request -- so there is no reason left to
#: query amazon.in, flipkart.com etc. directly and run into their bot walls.
#: The other adapters (amazon_in.py, flipkart.py, firstcry.py, hamleys.py,
#: shopify.py's Toycra/JaimanToys) are retired, not deleted: they still work,
#: still have tests and fixtures, and can be named explicitly with
#: --sources for a one-off manual check, but nothing calls them by default
#: any more. LEGO.com is excluded outright -- it renders prices client-side
#: only (see sources/lego_in.py) and BuyHatke does not track it either.
PRICE_SOURCES = ("buyhatke",)

#: The storefront the browsable catalogue is built from. Unaffected by the
#: BuyHatke move above: this is one clean, un-throttled JSON feed
#: (lego.mybrickhouse.com/products.json) used only to know WHAT sets exist,
#: their piece counts and images -- never a per-set price lookup, so it was
#: never the thing that ran into bot walls.
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
    "AmazonIN", "BuyHatke", "FirstCry", "Flipkart", "Hamleys", "JaimanToys", "LegoIN",
    "MyBrickHouse", "ShopifyStore", "Toycra",
    "Offer", "Source", "parse_inr",
    "SOURCE_CLASSES", "PRICE_SOURCES", "SEARCH_SOURCES", "RETAILER_LABELS",
    "CATALOG_SOURCE", "build_sources",
]
