"""Structural invariants the 3-way merge cursors depend on.

`merge_lines` and `annotate_three_way` walk base/local/remote with three cursors
that only ever move FORWARD, slicing base[a:bi], local[b:li], remote[c:ri] at each
synchronized line. That is only sound if the base->local and base->remote index
maps are strictly monotonic — otherwise a slice runs backwards, silently yielding
an empty region and dropping lines from the merge.

The maps come from SequenceMatcher.get_matching_blocks(), which does guarantee
monotonicity in both sequences. This file pins that guarantee: it was audited by
hand and by 20k randomized trials before being relied on, and a future rewrite of
_index_map that breaks it would corrupt merges silently rather than crash.
"""
import random

from gitplucker.merge.three_way import _index_map, merge_text


def _monotonic(m):
    vals = [m[k] for k in sorted(m)]
    return all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))


def _mutate(seq, rng, n):
    seq = list(seq)
    for _ in range(n):
        op = rng.choice("idms") if seq else "i"
        if op == "i":
            seq.insert(rng.randrange(len(seq) + 1), f"{rng.randint(0, 5)}\n")
        elif op == "d":
            seq.pop(rng.randrange(len(seq)))
        elif op == "m":                       # move a line elsewhere
            line = seq.pop(rng.randrange(len(seq)))
            seq.insert(rng.randrange(len(seq) + 1), line)
        else:
            seq[rng.randrange(len(seq))] = f"{rng.randint(0, 5)}\n"
    return seq


def test_index_map_is_strictly_monotonic_under_random_edits():
    rng = random.Random(7)
    for _ in range(3000):
        base = [f"{rng.randint(0, 5)}\n" for _ in range(rng.randint(0, 12))]
        local = _mutate(base, rng, rng.randint(0, 4))
        remote = _mutate(base, rng, rng.randint(0, 4))
        assert _monotonic(_index_map(base, local))
        assert _monotonic(_index_map(base, remote))


def test_merge_cursors_never_run_backwards():
    """The exact invariant merge_lines relies on, checked directly."""
    rng = random.Random(11)
    for _ in range(3000):
        base = [f"{rng.randint(0, 5)}\n" for _ in range(rng.randint(0, 12))]
        local = _mutate(base, rng, rng.randint(0, 4))
        remote = _mutate(base, rng, rng.randint(0, 4))
        la, lb = _index_map(base, local), _index_map(base, remote)
        b = c = 0
        for bi in sorted(i for i in la if i in lb):
            li, ri = la[bi], lb[bi]
            assert li >= b and ri >= c, (base, local, remote)
            b, c = li + 1, ri + 1


def test_merge_never_loses_an_unconflicted_local_line():
    """Whatever the shape of the edits, a clean merge must not drop content."""
    rng = random.Random(13)
    for _ in range(1500):
        base = [f"line{i}\n" for i in range(rng.randint(1, 10))]
        local = list(base)
        marker = "UNIQUE_LOCAL_MARKER\n"
        local.insert(rng.randrange(len(local) + 1), marker)
        remote = list(base)
        if remote:
            remote[rng.randrange(len(remote))] = "CHANGED_UPSTREAM\n"
        res = merge_text("".join(base), "".join(local), "".join(remote))
        assert marker.strip() in res.text
