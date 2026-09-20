#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Interval-construction rules.
#
# A rule turns a set of event records into intervals. It is part of the
# measurement, not post-processing hygiene: two defensible rules applied to one
# task-switching stream gave 8.43 us and 27.41 us, a factor of 3.3, and nothing
# in either output revealed which had been used. Every rule therefore carries a
# name, a version and its parameters into the artefact it emits, and a version
# is bumped whenever behaviour changes.
#
# Rules here:
#   token-pair        two events with equal token, one opening and one closing.
#                     Works within one context and across two.
#   yield-successor   for an interval with no carryable token: the arrival is
#                     the next event in the merged stream from another context.
import sys

from mcib_derive import (TIER_UNJOINABLE, artefact, derive_tier, load_record,
                         summarise, write_artefact)

PT_OPEN, PT_CLOSE = 1, 2


def _events(path, want=("token", "point_id", "t_wall", "seq"), stream=0):
    """Events as tuples, plus where each one came from.

    The trailing (stream, row) pair is appended AFTER the fields a rule
    matches on, so every positional use of the earlier fields is unchanged.
    It exists because a consumer of the intervals — counter attribution — has
    to get back to the two event records an interval was built from, and the
    rule is the only thing that knows which two they were. Recomputing the
    pairing anywhere else would be a second path to the same number, and two
    paths drift."""
    hdr, cols, rows = load_record(path)
    ix = {c: i for i, c in enumerate(cols)}
    ctx = hdr["context"]
    out = [tuple([int(r[ix[w]]) for w in want] + [ctx, stream, row])
           for row, r in enumerate(rows)]
    return hdr, out


# ---------------------------------------------------------------- token-pair

TOKEN_PAIR_VERSION = 1
TOKEN_PAIR_PARAMS = {
    "open_point_id": PT_OPEN,
    "close_point_id": PT_CLOSE,
    "pairing_key": "token",
    "rejection_policy": (
        "a token carried by exactly one opening and one closing event is "
        "matched; a token seen once is unmatched; a token seen more than "
        "twice is rejected as ambiguous, since nothing in the stream says "
        "which occurrence belongs to which; a pair whose closing event does "
        "not follow its opening event in time is rejected as out of order"),
    "halve_round_trip": False,
}


def token_pair(paths, metric, halve=False, with_endpoints=False):
    """Pair events across all input records on an equal token.

    This is the rule a workload-carried token permits. Nothing in the hot path
    helps it: the token is the workload's own identity for the iteration, and
    the rule reads it back out of the records afterwards. Where the opening and
    closing events are in different contexts, the pairing is cross-context and
    the tier decides whether the interval may be emitted at all."""
    headers, streams = [], []
    for si, p in enumerate(paths):
        h, e = _events(p, stream=si)
        headers.append(h)
        streams.append(e)

    tier, evidence = derive_tier(headers)

    by_token = {}
    for e in [x for s in streams for x in s]:
        by_token.setdefault(e[0], []).append(e)

    intervals = []
    endpoints = []
    unmatched = {}
    rejected = {}
    cross = 0

    for tok, evs in by_token.items():
        opens = [e for e in evs if e[1] == PT_OPEN]
        closes = [e for e in evs if e[1] == PT_CLOSE]
        if len(evs) > 2:
            rejected["token_seen_more_than_twice"] = \
                rejected.get("token_seen_more_than_twice", 0) + 1
            continue
        if not opens:
            unmatched["closing_without_opening"] = \
                unmatched.get("closing_without_opening", 0) + 1
            continue
        if not closes:
            unmatched["opening_without_closing"] = \
                unmatched.get("opening_without_closing", 0) + 1
            continue
        o, c = opens[0], closes[0]
        if c[2] < o[2]:
            rejected["closing_before_opening"] = \
                rejected.get("closing_before_opening", 0) + 1
            continue
        if o[4] != c[4]:
            cross += 1
        intervals.append(c[2] - o[2])
        endpoints.append((o[5], o[6], c[5], c[6]))

    if tier == TIER_UNJOINABLE and cross:
        raise SystemExit(
            "refusing to emit %d cross-domain intervals at tier unjoinable: "
            "the records do not share a time base and no offset was measured"
            % cross)

    hz = int(headers[0]["domain.frequency_hz"])
    ns = [v * 1e9 / hz for v in intervals]
    if halve:
        ns = [v / 2.0 for v in ns]

    params = dict(TOKEN_PAIR_PARAMS, halve_round_trip=bool(halve))
    pairing = {
        "matched": len(intervals),
        "cross_context": cross,
        "unmatched": {"count": sum(unmatched.values()), "by_reason": unmatched},
        "rejected": {"count": sum(rejected.values()), "by_clause": rejected},
    }
    art = artefact("token-pair", TOKEN_PAIR_VERSION, params, paths, headers,
                   tier, evidence, pairing, ns, metric)
    return (art, ns, endpoints) if with_endpoints else (art, ns)


# ----------------------------------------------------------- yield-successor

YIELD_SUCCESSOR_VERSION = 1
YIELD_SUCCESSOR_PARAMS = {
    "open_point_id": PT_OPEN,
    "close_point_id": PT_CLOSE,
    "tie_break": "at equal timestamps the opening point sorts before the "
                 "closing point",
    "rejection_policy": (
        "the arrival is the immediately following event in the merged stream; "
        "if it belongs to the same context the yield produced no switch and "
        "is rejected; the rule does NOT scan past it, because in a clean "
        "alternation a same-context successor is the steady state and "
        "scanning past it matches across a half cycle"),
    "lookahead": 1,
}


def yield_successor(paths, metric, with_endpoints=False):
    """For an interval no context owns and no token can cross."""
    if len(paths) != 2:
        raise SystemExit("yield-successor takes exactly two records")
    headers, streams = [], []
    for si, p in enumerate(paths):
        h, e = _events(p, stream=si)
        headers.append(h)
        streams.append(e)

    tier, evidence = derive_tier(headers)
    merged = sorted([x for s in streams for x in s], key=lambda e: (e[2], e[1]))

    intervals = []
    endpoints = []
    unmatched = {}
    rejected = {}
    arrival_was_open = 0

    for i, e in enumerate(merged):
        if e[1] != PT_OPEN:
            continue
        if i + 1 >= len(merged):
            unmatched["no_successor"] = unmatched.get("no_successor", 0) + 1
            continue
        nxt = merged[i + 1]
        if nxt[4] == e[4]:
            rejected["same_context_successor_no_switch"] = \
                rejected.get("same_context_successor_no_switch", 0) + 1
            continue
        if nxt[1] == PT_OPEN:
            arrival_was_open += 1
        intervals.append(nxt[2] - e[2])
        endpoints.append((e[5], e[6], nxt[5], nxt[6]))

    if tier == TIER_UNJOINABLE and intervals:
        raise SystemExit("refusing to emit cross-domain intervals at tier "
                         "unjoinable")

    hz = int(headers[0]["domain.frequency_hz"])
    ns = [v * 1e9 / hz for v in intervals]
    pairing = {
        "matched": len(intervals),
        "cross_context": len(intervals),
        "arrival_marked_by_opening_point": arrival_was_open,
        "unmatched": {"count": sum(unmatched.values()), "by_reason": unmatched},
        "rejected": {"count": sum(rejected.values()), "by_clause": rejected},
    }
    art = artefact("yield-successor", YIELD_SUCCESSOR_VERSION,
                   YIELD_SUCCESSOR_PARAMS, paths, headers, tier, evidence,
                   pairing, ns, metric)
    return (art, ns, endpoints) if with_endpoints else (art, ns)


RULES = {"token-pair": token_pair, "yield-successor": yield_successor}


def apply_declared(art, paths, with_endpoints=False):
    """Re-apply the rule an artefact declares, with the parameters it declares.

    An artefact states the rule, its version and its parameters precisely so
    that it can be rebuilt from the records it names. Anything that needs the
    pairing rather than the intervals — counter attribution needs the pair of
    events each interval was built from, to difference their counter
    snapshots — comes through here, so that there is exactly one
    implementation of each rule and no consumer reconstructs one of its own.

    Raises on a rule this module does not implement, rather than guessing:
    an artefact naming an unknown rule cannot be attributed."""
    name = art["rule"]["name"]
    if name not in RULES:
        raise ValueError("artefact names rule %r, which is not implemented "
                         "here; it cannot be rebuilt" % name)
    declared = art["rule"].get("version")
    implemented = (TOKEN_PAIR_VERSION if name == "token-pair"
                   else YIELD_SUCCESSOR_VERSION)
    if declared != implemented:
        raise ValueError(
            "artefact was built by %s v%s but v%s is implemented here; "
            "rebuilding it would compare two different rules"
            % (name, declared, implemented))
    if name == "token-pair":
        halve = bool(art["rule"]["parameters"].get("halve_round_trip"))
        return token_pair(paths, art["metric"], halve,
                          with_endpoints=with_endpoints)
    return yield_successor(paths, art["metric"], with_endpoints=with_endpoints)


def main(argv):
    if len(argv) < 4:
        print("usage: rules.py <rule> <metric> <out.json> <record...> "
              "[--halve]\n  rules: %s" % ", ".join(sorted(RULES)),
              file=sys.stderr)
        return 2
    rule, metric, out = argv[1], argv[2], argv[3]
    halve = "--halve" in argv
    paths = [a for a in argv[4:] if not a.startswith("--")]
    if rule not in RULES:
        print("unknown rule %s" % rule, file=sys.stderr)
        return 2
    art, ns = (token_pair(paths, metric, halve) if rule == "token-pair"
               else yield_successor(paths, metric))
    write_artefact(art, out, values=[int(round(v)) for v in ns])
    summarise(art)
    print("  artefact: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
