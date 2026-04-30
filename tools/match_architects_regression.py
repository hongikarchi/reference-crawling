"""Regression: compare 4-source matcher's metalocus↔divisare pairings against
the existing 2-source matcher's verdicts (1,489 auto-accept rows).

Pass criterion: agree ≥ 90%.
"""
import json
import sys


OLD_PATH = "data/canonical/match/metalocus_architect_to_divisare.json"
NEW_PATH = "data/canonical/architects_canonical.json"


def main() -> int:
    old = json.load(open(OLD_PATH))
    new = json.load(open(NEW_PATH))

    old_pairs: dict[str, int] = {}
    for m in old["matches"]:
        if m["verdict"] in ("auto_accept", "accept_with_country") and m["divisare_id"]:
            old_pairs[m["metaloc_id"]] = m["divisare_id"]

    # From new clusters, extract metalocus↔divisare pairings
    new_pairs: dict[str, set[int]] = {}  # one metaloc_id may link to many div_ids if cluster has both
    for c in new["clusters"]:
        srcs = c["source_refs"]
        if "metalocus" in srcs and "divisare" in srcs:
            for met_id in srcs["metalocus"]:
                for div_id in srcs["divisare"]:
                    new_pairs.setdefault(met_id, set()).add(int(div_id))

    agree = sum(1 for k, v in old_pairs.items() if v in new_pairs.get(k, set()))
    disagree = sum(1 for k, v in old_pairs.items()
                   if k in new_pairs and v not in new_pairs[k])
    missing = sum(1 for k in old_pairs if k not in new_pairs)
    total = len(old_pairs)

    print(f"Old 2-source verdicts (auto-accept): {total}")
    print(f"  agree    : {agree:>5} ({agree/total:.1%})")
    print(f"  disagree : {disagree:>5} ({disagree/total:.1%})")
    print(f"  missing  : {missing:>5} ({missing/total:.1%})")
    pass_rate = agree / total if total else 0
    if pass_rate >= 0.90:
        print(f"\n✓ PASS — agreement {pass_rate:.1%} ≥ 90%")
        return 0
    print(f"\n✗ FAIL — agreement {pass_rate:.1%} < 90%")
    if disagree > 0:
        print("\nFirst 10 disagreements (old verdict ≠ new cluster):")
        n = 0
        for k, old_v in old_pairs.items():
            if k in new_pairs and old_v not in new_pairs[k]:
                print(f"  metaloc {k}: old div={old_v} new div={new_pairs[k]}")
                n += 1
                if n >= 10:
                    break
    if missing > 0:
        print("\nFirst 10 missing (in old verdict, not in new cluster):")
        n = 0
        for k in old_pairs:
            if k not in new_pairs:
                print(f"  metaloc {k} → old div {old_pairs[k]}")
                n += 1
                if n >= 10:
                    break
    return 1


if __name__ == "__main__":
    sys.exit(main())
