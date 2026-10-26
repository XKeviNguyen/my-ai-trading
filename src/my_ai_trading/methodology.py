"""Methodology catalog (PRD F04) and the applicability gate (PRD F03/F13).

Profiles are *research profiles inspired by publicly described methodologies*, not reproductions
of anyone's private system. Timeframes are filled in only where a public source supports them;
otherwise they are "UNVERIFIED". The gate runs before any evaluation and is deterministic and free.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .domain import AnalysisMode, Applicability, InstrumentIdentity

UNVERIFIED = "UNVERIFIED"
CATALOG_VERSION = "catalog/1"

# Data a platform may hold. V0.2 has public OHLCV only.
OHLCV = "ohlcv"
FUNDAMENTALS = "company_fundamentals"
MACRO = "macro_data"
RS_UNIVERSE = "relative_strength_universe"
CATALYSTS = "news_catalysts"
SPREADS = "futures_spreads_curves"
AVAILABLE_DATA_DEFAULT = frozenset({OHLCV})

LABELS = {
    AnalysisMode.NATIVE: "Methodology-inspired / native scope",
    AnalysisMode.HYBRID: "Hybrid: native context + user timing",
    AnalysisMode.ADAPTED: "User-adapted — unvalidated; not the trader's original system",
    AnalysisMode.CUSTOM: "Custom strategy",
}


@dataclass(frozen=True)
class Profile:
    methodology_id: str
    name: str
    focus: str
    published_assets: tuple[str, ...]
    documented_timeframes: tuple[str, ...]        # () means unknown -> UNVERIFIED
    context_timeframes: tuple[str, ...]
    execution_timeframes: tuple[str, ...] | str   # UNVERIFIED when sources are vague
    required_data: tuple[str, ...]
    evidence_quality: str                         # published_rules | published_book | secondary_public | unverified
    provenance: str
    implementation_maturity: str = "catalog_only"  # catalog_only | experimental | validated
    evaluator: str | None = None
    limitations: tuple[str, ...] = ()
    strategy_version: str = "1"
    market_calendar: str = "source-dependent; crypto evaluated on 24x7 UTC bars"
    optional_data: tuple[str, ...] = ()           # missing -> affected criteria reported unevaluated

    @property
    def price_only(self) -> bool:
        return set(self.required_data) <= {OHLCV}

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["documented_timeframes"] = list(self.documented_timeframes) or UNVERIFIED
        d["price_only"] = self.price_only
        d["catalog_version"] = CATALOG_VERSION
        return d


_P = Profile
PROFILES: tuple[Profile, ...] = (
    _P("minervini", "Mark Minervini", "SEPA, VCP, trend template, breakout and volume", ("equity",),
       ("1d",), ("1w",), (), (OHLCV,), "published_book",
       "Trade Like a Stock Market Wizard (2013): trend template on daily charts with 50/150/200-day MAs.",
       "experimental", "minervini_trend_template",
       ("Only the price/MA part of the trend template is evaluated; RS rating and SEPA fundamentals are not.",
        "VCP detection is not implemented; a contraction event is not a VCP."),
       optional_data=(RS_UNIVERSE, FUNDAMENTALS)),
    _P("brandt", "Peter Brandt", "Classical chart patterns, market structure, invalidation",
       ("futures", "fx", "crypto"), ("1d", "1w"), ("1w",), (), (OHLCV,), "published_book",
       "Diary of a Professional Commodity Trader (2011): classical patterns on daily/weekly charts.",
       limitations=("Pattern recognition is discretionary; no mechanical rule set is published.",)),
    _P("livermore", "Jesse Livermore", "Pivotal points, trend behaviour, price leadership",
       ("equity", "commodity"), ("1d",), (), (), (OHLCV,), "published_book",
       "How to Trade in Stocks (1940): pivotal points from daily price records."),
    _P("oneil", "William J. O'Neil", "CAN SLIM, growth and institutional accumulation", ("equity",),
       ("1d", "1w"), ("1w",), (), (OHLCV, FUNDAMENTALS, RS_UNIVERSE), "published_book",
       "How to Make Money in Stocks: CAN SLIM uses earnings data plus daily/weekly charts."),
    _P("weinstein", "Stan Weinstein", "Stage analysis with the 30-week moving average", ("equity",),
       ("1w",), (), (), (OHLCV,), "published_book",
       "Secrets for Profiting in Bull and Bear Markets (1988): weekly charts, 30-week MA, volume.",
       "experimental", "weinstein_stage",
       ("Stage boundaries use explicit slope thresholds chosen here; the book describes them visually.",
        "Originally published for equities; crypto application is an extension.")),
    _P("darvas", "Nicolas Darvas", "Darvas box breakouts", ("equity",), ("1d",), (), (), (OHLCV,),
       "published_book", "How I Made $2,000,000 in the Stock Market (1960): boxes from daily highs/lows.",
       "experimental", "darvas_box",
       ("Box confirmation uses the commonly cited three-day rule; Darvas also required new highs and "
        "volume, which are reported but not enforced.",)),
    _P("kullamagi", "Kristjan Kullamägi", "Momentum bursts, episodic pivots", ("equity",), ("1d",), (),
       UNVERIFIED, (OHLCV, CATALYSTS), "secondary_public",
       "Public interviews and posts; intraday entry intervals are not verified."),
    _P("dennis", "Richard Dennis", "Turtle trend-following principles", ("futures",), ("1d",), (), (),
       (OHLCV,), "published_rules",
       "The Original Turtle Trading Rules (C. Faith, 2003) and Way of the Turtle (2007), written by "
       "former Turtles, not by Dennis.", "experimental", "turtle_breakout",
       ("Evaluated on closed daily bars; the original rules entered intraday on the breakout price.",
        "Position sizing and portfolio limits are not modelled.")),
    _P("seykota", "Ed Seykota", "Systematic trends, risk and position sizing", ("futures",), (), (),
       UNVERIFIED, (OHLCV,), "unverified",
       "Interviews (Market Wizards); exact system rules and intervals are not public."),
    _P("raschke", "Linda Bradford Raschke", "Short-term and swing setups", ("futures", "equity"),
       ("1d",), (), UNVERIFIED, (OHLCV,), "published_book",
       "Street Smarts (1995, with L. Connors): daily setups; intraday variants not verified here."),
    _P("bnf", "Takashi Kotegawa (B.N.F.)", "Short-term reactions, deviation from moving average",
       ("equity",), ("1d",), (), UNVERIFIED, (OHLCV,), "secondary_public",
       "Media interviews describe deviation from the 25-day MA; no primary written rules."),
    _P("cis", "cis (Japan)", "Tactical trend/momentum and position management", ("equity",), (), (),
       UNVERIFIED, (OHLCV,), "unverified", "Media coverage and posts; no documented rule set."),
    _P("williams", "Larry Williams", "Short-term price behaviour and volatility breakouts", ("futures",),
       ("1d",), (), (), (OHLCV,), "published_book",
       "Long-Term Secrets to Short-Term Trading (1999): daily-bar volatility breakouts."),
    _P("breitstein", "Lance Breitstein", "Intraday catalysts, execution and risk", ("equity",), (), (),
       UNVERIFIED, (OHLCV, CATALYSTS), "secondary_public", "Public talks/interviews; intraday focus."),
    _P("hougaard", "Tom Hougaard", "Price action and trading psychology", ("equity_index",), (), (),
       UNVERIFIED, (OHLCV,), "published_book",
       "Best Loser Wins (2022) focuses on psychology; no mechanical rules or intervals published."),
    _P("ptj", "Paul Tudor Jones", "Global macro, asymmetric risk", ("multi_asset",), (), (), UNVERIFIED,
       (OHLCV, MACRO), "secondary_public", "Interviews; macro inputs required."),
    _P("soros", "George Soros", "Macro analysis and reflexivity", ("multi_asset",), (), (), UNVERIFIED,
       (MACRO,), "published_book", "The Alchemy of Finance (1987): reflexivity theory, not a rule set."),
    _P("druckenmiller", "Stanley Druckenmiller", "Macro trends, concentrated asymmetry", ("multi_asset",),
       (), (), UNVERIFIED, (OHLCV, MACRO), "secondary_public", "Interviews; liquidity/macro inputs required."),
    _P("chiu", "Kelvin Chiu", "Commodity and relative-value opportunities", ("commodity",), (), (),
       UNVERIFIED, (OHLCV, SPREADS), "unverified", "Limited public documentation."),
    _P("lynch", "Peter Lynch", "Fundamental growth investing and valuation", ("equity",), (), (),
       UNVERIFIED, (FUNDAMENTALS,), "published_book",
       "One Up on Wall Street (1989): company fundamentals and valuation; not a chart method."),
)
BY_ID = {p.methodology_id: p for p in PROFILES}
if len(PROFILES) != 20 or len(BY_ID) != 20:
    raise RuntimeError("Catalog must contain 20 unique profiles")


def asset_class(instrument: InstrumentIdentity) -> str:
    return "crypto" if instrument.venue in ("binance", "synthetic") else "unknown"


@dataclass
class Assessment:
    methodology_id: str
    name: str
    requested_mode: str
    status: Applicability
    label: str
    evaluation_timeframe: str | None
    analysis_timeframe: str
    reasons: list[str] = field(default_factory=list)
    offered_routes: list[dict[str, str]] = field(default_factory=list)
    requires_user_approval: bool = False
    evaluate: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = str(self.status)
        return d


def assess(profile: Profile, instrument: InstrumentIdentity, analysis_timeframe: str,
           mode: AnalysisMode = AnalysisMode.NATIVE, *,
           available_data: frozenset[str] = AVAILABLE_DATA_DEFAULT,
           approved_adaptation: bool = False) -> Assessment:
    """Decide whether and how a profile may be evaluated. Never returns a trade direction."""
    if mode is AnalysisMode.CUSTOM:
        raise ValueError("CUSTOM mode belongs to user strategies, not named methodologies")
    a = Assessment(profile.methodology_id, profile.name, str(mode), Applicability.UNVERIFIED_METHOD,
                   LABELS[mode], None, analysis_timeframe)
    asset = asset_class(instrument)

    if FUNDAMENTALS in profile.required_data and asset == "crypto":
        a.status = Applicability.UNSUPPORTED_ASSET
        a.label = "Not applicable / insufficient evidence"
        a.reasons.append(f"{profile.name}'s approach needs company financial statements; a crypto "
                         f"{instrument.market_type} has none, and OHLCV cannot substitute for them.")
        a.offered_routes.append({"mode": "CUSTOM", "description": "Create an independent technical strategy "
                                 f"on {analysis_timeframe} with no {profile.name} attribution."})
        return a

    missing = [d for d in profile.required_data if d not in available_data]
    if not profile.documented_timeframes:
        a.status = Applicability.UNVERIFIED_METHOD
        a.label = "Not applicable / insufficient evidence"
        a.reasons.append("No public source documents this method's timeframe or rules well enough to "
                         "evaluate; anything produced would be invented.")
        if missing:
            a.reasons.append("Also requires unavailable data: " + ", ".join(missing))
        a.offered_routes.append({"mode": "CUSTOM", "description": "Write your own rules as a custom strategy."})
        return a

    native_tf = profile.documented_timeframes[0]
    if missing:
        a.status = Applicability.INSUFFICIENT_DATA
        a.label = "Not applicable / insufficient evidence"
        a.reasons.append("Required data not available on this platform: " + ", ".join(missing))
        return a
    missing_optional = [d for d in profile.optional_data if d not in available_data]
    if missing_optional:
        a.reasons.append("Partially evaluable: missing " + ", ".join(missing_optional) + "; affected "
                         "criteria are reported as unevaluated, never assumed.")
    if asset not in profile.published_assets:
        a.reasons.append(f"Published for {', '.join(profile.published_assets)}; applying it to {asset} is an "
                         "extension, not part of the original source.")

    non_native = analysis_timeframe not in profile.documented_timeframes
    if non_native:
        a.reasons.append(f"You are viewing {analysis_timeframe}; {profile.name}'s documented timeframe is "
                         f"{'/'.join(profile.documented_timeframes)}. A {analysis_timeframe} reading is not "
                         "this methodology's native signal (e.g. 30 weekly bars ≠ 30 hourly bars).")
        a.offered_routes = [
            {"mode": "NATIVE", "description": f"Evaluate on {native_tf} as documented."},
            {"mode": "HYBRID", "description": f"{native_tf} methodology context + separately labelled "
                                              f"{analysis_timeframe} tactical rules you define."},
            {"mode": "ADAPTED", "description": f"Fork a versioned, unvalidated {analysis_timeframe} adaptation "
                                               "(preview + approval required; original stays unchanged)."},
        ]

    if mode is AnalysisMode.NATIVE:
        a.status, a.evaluation_timeframe = Applicability.SUPPORTED_NATIVE, native_tf
    elif mode is AnalysisMode.HYBRID:
        if not profile.price_only and not profile.evaluator:
            a.status = Applicability.INSUFFICIENT_DATA
            a.reasons.append("Hybrid needs the native context to be computable from available data.")
            return a
        a.status, a.evaluation_timeframe = Applicability.SUPPORTED_HYBRID, native_tf
        a.reasons.append(f"Context from {native_tf} ({profile.name}-inspired); {analysis_timeframe} timing is "
                         "a separate custom layer and carries no trader attribution.")
    else:  # ADAPTED
        a.evaluation_timeframe = analysis_timeframe
        a.status = Applicability.ADAPTED_UNVALIDATED
        if not approved_adaptation:
            a.requires_user_approval = True
            a.reasons.append("An adaptation must be forked as a new strategy version, previewed and approved "
                             "before it runs. No automatic mapping such as 30-week MA → 30-hour MA.")
            return a

    if profile.implementation_maturity == "catalog_only" or not profile.evaluator:
        a.reasons.append("Catalog only: rules are not implemented, so no evaluation is produced "
                         "(educational context only).")
        return a
    if mode is AnalysisMode.ADAPTED:
        a.reasons.append("Adapted evaluation is not implemented for built-in evaluators; create it as a "
                         "custom rule fork instead.")
        return a
    a.evaluate = True
    return a
