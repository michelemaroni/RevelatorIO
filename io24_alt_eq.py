#!/usr/bin/env python3
"""Exact UC 4.7.2 Passive and Vintage EQ support for the Linux Host.

Universal Control's component XML supplies the semantic controls and defaults.
The pinned ``dspusbdevice.dll`` supplies the coefficient designers.  The DLL is
read as data by the bounded interpreter in :mod:`io24_uc472_vintage_eq`; it is
never loaded or executed by the operating system.  Live routing follows UC's
recompute call sites exactly: one seven-float ``Lfdf`` section at index 0 and
three ``Bqdf`` sections at indexes 1 through 3.
"""

import cmath
import functools
import math
import os
from pathlib import Path

import io24_uc472_passive_eq as passive
import io24_uc472_vintage_eq as vintage


PASSIVE_CLASS_ID = passive.PASSIVE_CLASS_ID
VINTAGE_CLASS_ID = vintage.VINTAGE_CLASS_ID
CLASS_IDS = {"passive": PASSIVE_CLASS_ID, "vintage": VINTAGE_CLASS_ID}
MODEL_BY_CLASS_ID = {value: key for key, value in CLASS_IDS.items()}

PASSIVE_LOW_HZ = passive.LOW_FREQUENCIES
PASSIVE_HIGH_BOOST_HZ = passive.HIGH_BOOST_FREQUENCIES
PASSIVE_HIGH_ATTEN_HZ = passive.HIGH_ATTENUATION_FREQUENCIES
VINTAGE_LOW_HZ = vintage.LOW_FREQUENCIES
VINTAGE_LOWMID_HZ = vintage.LOW_MID_FREQUENCIES
VINTAGE_HIMID_HZ = vintage.HI_MID_FREQUENCIES

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0)
WIDE_IDENTITY = IDENTITY + (0.0, 0.0)
NATIVE_RATES = vintage.NATIVE_RATES

# Exact defaults and ordering from UC 4.7.2's embedded fatchannelxt.xml.
PASSIVE_DEFAULT = {
    "__classid": PASSIVE_CLASS_ID,
    "eqallon": 0,
    "bboost": 0.0,
    "batten": 0.0,
    "bfreq": 0,
    "mboost": 1.5,
    "bbwidth": 10.0,
    "mfreq": 4,
    "hatten": 1.5,
    "hsfreq": 2,
}
VINTAGE_DEFAULT = {
    "__classid": VINTAGE_CLASS_ID,
    "eqallon": 0,
    "lowgain": 0.0,
    "lowfreq": 1,
    "lowmidgain": 0.0,
    "lowmidfreq": 2,
    "himidgain": 0.0,
    "himidfreq": 2,
    "higain": 0.0,
}

DEFAULT_DLL = Path(os.environ.get(
    "XDG_CACHE_HOME", str(Path.home() / ".cache")
)) / "io24" / "re" / "dspusbdevice.dll"


class AlternateEqUnavailable(RuntimeError):
    """The exact retained UC designer dependency is unavailable."""


def default_eq(model, on=False):
    """Return a fresh exact UC semantic record for ``model``."""
    if model == "passive":
        result = dict(PASSIVE_DEFAULT)
    elif model == "vintage":
        result = dict(VINTAGE_DEFAULT)
    else:
        raise ValueError("alternate EQ model must be passive or vintage")
    result["eqallon"] = int(bool(on))
    return result


def model_of(eq):
    """Resolve a complete alternate model without guessing across classes."""
    if not isinstance(eq, dict):
        raise ValueError("alternate EQ must be an object")
    try:
        return MODEL_BY_CLASS_ID[eq.get("__classid")]
    except KeyError:
        raise ValueError("alternate EQ has an unknown class id")


def _number(eq, field, low, high, integer=False):
    if field not in eq or isinstance(eq[field], bool) and field != "eqallon":
        raise ValueError("%s EQ %s must be a finite number" %
                         (model_of(eq).title(), field))
    try:
        value = float(eq[field])
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s EQ %s must be a finite number" %
                         (model_of(eq).title(), field))
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("%s EQ %s must be in [%g, %g]" %
                         (model_of(eq).title(), field, low, high))
    if integer and not value.is_integer():
        raise ValueError("%s EQ %s must be an integer switch index" %
                         (model_of(eq).title(), field))
    return int(value) if integer else value


def validate_eq(eq):
    """Validate and normalize one complete UC alternate-EQ semantic record."""
    model = model_of(eq)
    if model == "passive":
        normalized = {
            "__classid": PASSIVE_CLASS_ID,
            "eqallon": _number(eq, "eqallon", 0, 1, True),
            "bboost": _number(eq, "bboost", 0, 10),
            "batten": _number(eq, "batten", 0, 10),
            "bfreq": _number(eq, "bfreq", 0, 3, True),
            "mboost": _number(eq, "mboost", 0, 10),
            "bbwidth": _number(eq, "bbwidth", 0, 10),
            "mfreq": _number(eq, "mfreq", 0, 6, True),
            "hatten": _number(eq, "hatten", 0, 10),
            "hsfreq": _number(eq, "hsfreq", 0, 2, True),
        }
    else:
        normalized = {
            "__classid": VINTAGE_CLASS_ID,
            "eqallon": _number(eq, "eqallon", 0, 1, True),
            "lowgain": _number(eq, "lowgain", -16, 16),
            "lowfreq": _number(eq, "lowfreq", 0, 3, True),
            "lowmidgain": _number(eq, "lowmidgain", -16, 16),
            "lowmidfreq": _number(eq, "lowmidfreq", 0, 2, True),
            "himidgain": _number(eq, "himidgain", -16, 16),
            "himidfreq": _number(eq, "himidfreq", 0, 2, True),
            "higain": _number(eq, "higain", -16, 16),
        }
    unknown = sorted(set(eq) - set(normalized))
    if unknown:
        raise ValueError("%s EQ has unknown field%s: %s" %
                         (model.title(), "s" if len(unknown) != 1 else "",
                          ", ".join(unknown)))
    return normalized


def resolve_dll(path=None):
    """Resolve the pinned UC artifact, supporting an explicit legal local copy."""
    selected = path or os.environ.get("IO24_UC472_DSPUSBDEVICE") or DEFAULT_DLL
    selected = Path(selected).expanduser().resolve()
    if not selected.is_file():
        raise AlternateEqUnavailable(
            "exact Passive/Vintage EQ needs the retained UC 4.7.2 "
            "dspusbdevice.dll; set IO24_UC472_DSPUSBDEVICE to your local copy")
    return selected


@functools.lru_cache(maxsize=4)
def _image(path):
    try:
        return vintage.Image(path)
    except (OSError, vintage.AnalysisError) as error:
        raise AlternateEqUnavailable(str(error))


def designer_status(path=None):
    """Return ``(available, detail)`` without touching USB or executing the DLL."""
    try:
        resolved = resolve_dll(path)
        _image(str(resolved))
    except (AlternateEqUnavailable, ImportError) as error:
        return False, str(error)
    return True, "pinned UC 4.7.2 designer available"


def _rate(rate_hz):
    if isinstance(rate_hz, bool):
        raise ValueError("EQ sample rate must be a finite number")
    try:
        rate = float(rate_hz)
    except (TypeError, ValueError):
        raise ValueError("EQ sample rate must be a finite number")
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("EQ sample rate must be positive")
    return rate


def design_live_sections(eq, rate_hz=48000.0, dll_path=None):
    """Return UC's exact live packet order as ``(kind, index, coefficients)``.

    The result is ready for :class:`io24.Io24`: ``kind`` is ``wide`` for the
    seven-float Lfdf section and ``biquad`` for each five-float Bqdf section.
    A bypassed model is exact identity and therefore needs no DLL access.
    """
    eq = validate_eq(eq)
    model = model_of(eq)
    rate = _rate(rate_hz)
    if not eq["eqallon"]:
        return (("wide", 0, WIDE_IDENTITY),
                ("biquad", 1, IDENTITY),
                ("biquad", 2, IDENTITY),
                ("biquad", 3, IDENTITY))

    image = _image(str(resolve_dll(dll_path)))
    if model == "passive":
        high = tuple(passive.design_high(
            image, eq["mfreq"], eq["mboost"], eq["hatten"], eq["hsfreq"],
            eq["bbwidth"], rate)["coefficients"])
        low = tuple(passive.design_low(
            image, eq["bfreq"], eq["bboost"], eq["batten"], rate
        )["coefficients"])
        return (("wide", 0, high), ("biquad", 1, low),
                ("biquad", 2, IDENTITY), ("biquad", 3, IDENTITY))

    low = tuple(vintage.design(
        image, "low", eq["lowfreq"], eq["lowgain"], rate)["coefficients"])
    high = tuple(vintage.design(
        image, "high", 0, eq["higain"], rate)["coefficients"])
    high_mid = tuple(vintage.design(
        image, "hi-mid", eq["himidfreq"], eq["himidgain"], rate
    )["coefficients"])
    low_mid = tuple(vintage.design(
        image, "low-mid", eq["lowmidfreq"], eq["lowmidgain"], rate
    )["coefficients"])
    return (("wide", 0, low), ("biquad", 1, high),
            ("biquad", 2, high_mid), ("biquad", 3, low_mid))


def design_native_coefficients(eq, dll_path=None):
    """Return the four-rate coefficient table used by a device block."""
    eq = validate_eq(eq)
    image = _image(str(resolve_dll(dll_path)))
    if model_of(eq) == "passive":
        return passive.design_eq(image, eq)
    return vintage.design_eq(image, eq)


def section_response(section, frequency_hz, rate_hz):
    """Complex response of one UC wire-format section."""
    coefficients = tuple(section)
    if len(coefficients) not in (5, 7):
        raise ValueError("EQ section must contain five or seven coefficients")
    padded = coefficients + (0.0,) * (7 - len(coefficients))
    b0, na1, b1, na2, b2, na3, b3 = padded
    z = cmath.exp(-2j * math.pi * float(frequency_hz) / _rate(rate_hz))
    numerator = b0 + b1 * z + b2 * z ** 2 + b3 * z ** 3
    denominator = 1.0 - na1 * z - na2 * z ** 2 - na3 * z ** 3
    return numerator / denominator


def response_db(sections, frequency_hz, rate_hz):
    """Combined dB response for a result from :func:`design_live_sections`."""
    response = complex(1.0, 0.0)
    for _kind, _index, coefficients in sections:
        response *= section_response(coefficients, frequency_hz, rate_hz)
    magnitude = abs(response)
    return -180.0 if magnitude <= 1e-9 else 20.0 * math.log10(magnitude)
