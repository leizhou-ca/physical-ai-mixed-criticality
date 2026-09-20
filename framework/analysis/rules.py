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
#
# WHERE THE COUNTER DELTA COMES FROM IS PART OF THE RULE. An interval's
# timestamps may span two contexts; its counter delta must not have to. A
# victim whose waiting context brackets its own block with two marks declares
# that pair as a COUNTER SEGMENT, and the rule returns it beside the interval
# it belongs to. The segment is always within one context, and no value is
# ever differenced across two.
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
        "matched; a token seen once is unmatched; a token carrying more than "
        "one opening or more than one closing event is rejected as "
        "ambiguous, since nothing in the stream says which occurrence "
        "belongs to which; a pair whose closing event does not follow its "
        "opening event in time is rejected as out of order. Events at any "
        "other instrumented point are not endpoints and are not counted "
        "here: they belong to the counter segment, if one is declared"),
    "halve_round_trip": False,
}


def token_pair(paths, metric, halve=False, with_endpoints=False,
               counter_segment=None, with_counter_segment=False):
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
    counter_eps = []
    unmatched = {}
    rejected = {}
    cross = 0

    # The declared counter segment, indexed by token. Both of its marks are
    # in ONE context, which is checked here rather than assumed: a segment
    # whose two ends were in different contexts would be the very thing this
    # is here to avoid.
    seg_index, seg_problems = {}, {}
    if counter_segment:
        want_ctx = counter_segment.get("context")
        s_open = counter_segment["open_point_id"]
        s_close = counter_segment["close_point_id"]
        for e in [x for st in streams for x in st]:
            if want_ctx is not None and e[4] != want_ctx:
                continue
            if e[1] == s_open:
                seg_index.setdefault(e[0], {})["open"] = e
            elif e[1] == s_close:
                seg_index.setdefault(e[0], {})["close"] = e

    for tok, evs in by_token.items():
        opens = [e for e in evs if e[1] == PT_OPEN]
        closes = [e for e in evs if e[1] == PT_CLOSE]
        # Counted over the ENDPOINTS, not over every event carrying the
        # token. On a record whose only points are the two endpoints the two
        # tests are the same; they differ once a victim marks a counter
        # segment, whose events carry the interval's token and are not
        # endpoints of it.
        if len(opens) > 1 or len(closes) > 1:
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
        if counter_segment:
            pair = seg_index.get(tok) or {}
            so, sc = pair.get("open"), pair.get("close")
            if so is None or sc is None:
                counter_eps.append(None)
                seg_problems["counter_segment_incomplete"] = \
                    seg_problems.get("counter_segment_incomplete", 0) + 1
            elif so[5] != sc[5]:
                counter_eps.append(None)
                seg_problems["counter_segment_spans_two_contexts"] = \
                    seg_problems.get("counter_segment_spans_two_contexts", 0) + 1
            elif so[2] > o[2]:
                # The segment must open before the interval does. If it does
                # not, the waiting context had not blocked when the interval
                # started, so the delta does not bracket a block.
                counter_eps.append(None)
                seg_problems["counter_segment_opens_inside_the_interval"] = \
                    seg_problems.get(
                        "counter_segment_opens_inside_the_interval", 0) + 1
            else:
                counter_eps.append((so[5], so[6], sc[5], sc[6]))

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
    # Declared only when the victim declares one, so that an artefact from a
    # victim with no counter segment is unchanged in every field.
    if counter_segment:
        params["counter_segment"] = dict(counter_segment)
        pairing["counter_segment"] = {
            "resolved": sum(1 for x in counter_eps if x is not None),
            "unresolved": sum(1 for x in counter_eps if x is None),
            "by_reason": seg_problems,
            "rule": ("the counter delta for an interval is taken between the "
                     "two declared points in the declared context; it is "
                     "never differenced across contexts"),
        }
    art = artefact("token-pair", TOKEN_PAIR_VERSION, params, paths, headers,
                   tier, evidence, pairing, ns, metric)
    if with_counter_segment:
        return art, ns, endpoints, (counter_eps if counter_segment else None)
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


def yield_successor(paths, metric, with_endpoints=False,
                    with_counter_segment=False):
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
    if with_counter_segment:
        # No counter segment is possible for a core-owned interval: neither
        # context is present at both ends, so no pair of marks in one context
        # brackets it. What this metric needs is a core-bound counter, which
        # is a backend question and not a rule one.
        return art, ns, endpoints, None
    return (art, ns, endpoints) if with_endpoints else (art, ns)


RULES = {"token-pair": token_pair, "yield-successor": yield_successor}


def apply_named(rule_name, parameters, paths, metric, with_endpoints=False,
                with_counter_segment=False):
    """Apply a rule by name, with the parameters a caller declares.

    This is the entry point a pipeline driver uses. It exists so that the
    driver knows a rule's NAME and nothing about how the rule works: which
    arguments token-pair takes, and that one metric's round trip is halved,
    are facts about the rule, and a driver that knew them would have to be
    edited whenever a rule gained one."""
    if rule_name not in RULES:
        raise ValueError("no such interval rule: %r; this build has %s"
                         % (rule_name, ", ".join(sorted(RULES))))
    parameters = parameters or {}
    if rule_name == "token-pair":
        return token_pair(paths, metric,
                          bool(parameters.get("halve_round_trip")),
                          with_endpoints=with_endpoints,
                          counter_segment=parameters.get("counter_segment"),
                          with_counter_segment=with_counter_segment)
    return yield_successor(paths, metric, with_endpoints=with_endpoints,
                           with_counter_segment=with_counter_segment)


def apply_declared(art, paths, with_endpoints=False,
                   with_counter_segment=False):
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
        params = art["rule"]["parameters"]
        halve = bool(params.get("halve_round_trip"))
        return token_pair(paths, art["metric"], halve,
                          with_endpoints=with_endpoints,
                          counter_segment=params.get("counter_segment"),
                          with_counter_segment=with_counter_segment)
    return yield_successor(paths, art["metric"], with_endpoints=with_endpoints,
                           with_counter_segment=with_counter_segment)


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
