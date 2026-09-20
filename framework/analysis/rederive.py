#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Re-derive existing artefacts in place, from the records they name.
#
# An artefact says which rule produced it, with which parameters, from which
# inputs — so it carries everything needed to reproduce itself. That is the
# property §5.12.2 exists to give, and this tool is what exercises it: run
# against a dataset it rebuilds every artefact and refuses to replace one
# whose reproduced fields differ from what is on disk.
#
# It is not a migration script. It replaces an artefact only when everything
# except the fields named in --expect-new is byte-identical, so a rule change
# that alters a published number stops it rather than being written silently
# over the evidence.
#
# Usage: rederive.py <derived-dir> [--expect-new field ...] [--apply]
#        without --apply it reports and changes nothing.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import token_pair, yield_successor        # noqa: E402
from mcib_derive import write_artefact               # noqa: E402


def rebuild(art, root):
    paths = [os.path.join(root, i["path"]) for i in art["inputs"]]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return None, None, "inputs missing: %s" % ", ".join(missing)
    rule = art["rule"]["name"]
    if rule == "token-pair":
        halve = bool(art["rule"]["parameters"].get("halve_round_trip"))
        new, ns = token_pair(paths, art["metric"], halve)
    elif rule == "yield-successor":
        new, ns = yield_successor(paths, art["metric"])
    else:
        return None, None, "unknown rule '%s'" % rule
    # The stored paths are relative to where the derive ran; keep them so a
    # re-derivation does not rewrite provenance into absolute paths.
    for old_in, new_in in zip(art["inputs"], new["inputs"]):
        new_in["path"] = old_in["path"]
    return new, ns, None


def main(argv):
    if len(argv) < 2:
        print("usage: rederive.py <derived-dir> [--expect-new f ...] [--apply]",
              file=sys.stderr)
        return 2
    derdir = argv[1]
    apply_ = "--apply" in argv
    expect = set()
    if "--expect-new" in argv:
        i = argv.index("--expect-new") + 1
        while i < len(argv) and not argv[i].startswith("--"):
            expect.add(argv[i]); i += 1
    root = os.path.dirname(os.path.abspath(derdir)) or "."

    files = sorted(f for f in os.listdir(derdir) if f.endswith(".json"))
    same = changed = failed = 0
    for f in files:
        p = os.path.join(derdir, f)
        old = json.load(open(p))
        new, ns, err = rebuild(old, root)
        if err:
            print("  FAILED  %-44s %s" % (f, err)); failed += 1; continue
        diffs = [k for k in set(old) | set(new)
                 if k not in expect and old.get(k) != new.get(k)]
        if diffs:
            print("  DIFFERS %-44s in %s" % (f, ", ".join(sorted(diffs))))
            changed += 1
            continue
        same += 1
        if apply_:
            # Written under a temporary name and renamed, so an interrupted
            # run cannot leave a dataset with a hole in it. write_artefact
            # refuses to overwrite, which is the behaviour a derive wants and
            # the wrong one for a verified in-place replacement.
            tmp = p + ".new"
            vtmp = tmp.replace(".json", ".values.csv")
            for q in (tmp, vtmp):
                if os.path.exists(q):
                    os.remove(q)
            write_artefact(new, tmp, values=[int(round(x)) for x in ns])
            os.replace(tmp, p)
            os.replace(vtmp, p.replace(".json", ".values.csv"))

    print("%s: %d reproduced identically, %d differ, %d failed%s"
          % (derdir, same, changed, failed,
             "  — rewritten" if apply_ and not changed and not failed else ""))
    return 1 if (changed or failed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
