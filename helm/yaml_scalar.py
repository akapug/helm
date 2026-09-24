"""The ONE scalar reader for helm's generated proxy config (task/2126).

Two copies of this reader — one in the launch-assets generator, one in the
watchdog — disagreed on 7 of 19 measured poles, and the worst disagreement
was silent: a single-quoted value with a trailing comment (the upstream
example file's own documented style) came back from the watchdog WITH ITS
QUOTES ON, a different string than the generator wrote, minting drift that
does not exist on a field nobody edited.

One reader, ONE typed answer: a VALUE, or a TYPED ABSENCE the caller
decides about. The plan side raises (an unreadable desired state must
refuse); the watchdog side reads absence as UNKNOWN and never mints drift.
"""
import json
import re


#: The ONE err that means NOT THERE rather than NOT READABLE. Callers that
#: tolerate absence and refuse on unreadability need to tell the two apart,
#: and keying on a bare string literal at each call site is how the two
#: copies this module replaced drifted in the first place.
ABSENT = "empty scalar"


def yaml_scalar(value):
    """-> (value, err): exactly one is None. err names the failure class so
    a caller refusing can say WHY, and a caller tolerating can distinguish
    "not there" from "not readable" — an empty scalar and an empty quoted
    string are both legitimate values, so absence is never a bare None."""
    value = value.strip()
    if not value:
        return None, ABSENT
    if value.startswith("#"):
        # A comment-only value is a blank scalar with a comment on it —
        # null in the producer's grammar, not text to read.
        return None, ABSENT
    if value.startswith('"'):
        # JSON's own decoder reads the quoted prefix AND tells us where it
        # ended, so a trailing comment survives and trailing junk refuses.
        try:
            scalar, end = json.JSONDecoder().raw_decode(value)
        except (TypeError, ValueError):
            return None, "malformed double-quoted scalar"
        tail = value[end:].strip()
        if not isinstance(scalar, str):
            return None, "double-quoted scalar is not a string"
        if tail and not tail.startswith("#"):
            return None, "trailing content after double-quoted scalar"
        return scalar, None
    if value.startswith("'"):
        # YAML single quotes escape by doubling; the comment is legal ONLY
        # after the closing quote. A startswith/endswith test misses that:
        # a trailing comment ends the line in a letter, so the reader falls
        # to its bare branch and returns the value with its quotes on.
        match = re.match(r"^'((?:[^']|'')*)'(?:\s+#.*)?$", value)
        if not match:
            return None, "malformed single-quoted scalar"
        return match.group(1).replace("''", "'"), None
    # ANY WHITESPACE OPENS A COMMENT, WHICH IS THE GENERATOR'S RULE AND THE
    # ONE YAML STATES. Unifying two readers means keeping the CORRECT copy,
    # and the first cut kept the other one: a single-space split leaves a
    # TAB-commented value with its comment glued on, so the generator would
    # have started shipping `abc\t#c` into config as the value `abc\t#c`.
    # The `+` matters too -- `abc  #c` puts two spaces before the hash.
    # A hash with NO whitespace before it is part of the scalar, which is
    # why this is not a bare split on "#".
    return re.split(r"[ \t]+#", value, maxsplit=1)[0].strip(), None


def yaml_scalar_or_raise(value):
    """The PLAN side's answer: a value, or a refusal."""
    scalar, err = yaml_scalar(value)
    if err is not None:
        raise ValueError(err)
    return scalar



def yaml_scalar_typed(value):
    """(value, err, quoted) — yaml_scalar, plus whether the value arrived
    QUOTED. The fork field's producer (CLIProxyAPI's yaml.v3 into a Go bool)
    rejects quoted booleans, so a watchdog interpreting fork from the
    stripped scalar alone would certify syntax the producer refuses. Every
    other caller should use yaml_scalar: quoting is type information, and
    only a boolean question can spend it."""
    stripped = value.strip()
    quoted = stripped.startswith(('"', "'"))
    scalar, err = yaml_scalar(value)
    return scalar, err, quoted
