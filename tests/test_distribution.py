"""Distributions de durées (Trace.STATistic.DISTribution) : réservoir, centiles, histogramme,
valeurs les plus longues."""
from gdbtrace.trace.distribution import Reservoir, distribution, percentile


def test_percentiles_nearest_rank():
    vals = list(range(1, 101))                       # 1..100
    assert (percentile(vals, 50), percentile(vals, 90), percentile(vals, 99)) == (50, 90, 99)
    assert percentile([7], 99) == 7


def test_reservoir_exact_below_capacity_and_longest():
    r = Reservoir(capacity=1000, top=3)
    for i, v in enumerate([5, 1, 9, 3, 9, 2]):
        r.add(v, where=100 + i)
    assert (r.count, r.min, r.max, r.total) == (6, 1, 9, 29) and not r.sampled
    assert r.longest() == [(9, 102), (9, 104), (5, 100)]   # à valeur égale : la première vue
    d = distribution("f", r, bins=4, log=False)
    assert [b.count for b in d.bins] == [2, 1, 1, 2] and sum(b.count for b in d.bins) == 6
    assert (d.bins[0].low, d.bins[-1].high) == (1, 9)
    assert d.percentiles == {50: 3, 90: 9, 99: 9} and d.mean == 29 / 6
    assert "6 valeurs" in d.summary()


def test_reservoir_sampling_keeps_exact_extremes():
    r = Reservoir(capacity=100, top=5)
    for i in range(10_000):
        r.add(i % 1000, where=i)
    assert r.count == 10_000 and len(r.values) == 100 and r.sampled
    assert (r.min, r.max) == (0, 999)
    assert [v for v, _w in r.longest()] == [999] * 5
    d = distribution("g", r, bins=10)
    assert sum(b.count for b in d.bins) == 10_000 and d.sampled
    assert 300 < d.percentiles[50] < 700                     # échantillon uniforme
    again = Reservoir(capacity=100, top=5)                   # graine fixe : même échantillon
    for i in range(10_000):
        again.add(i % 1000, where=i)
    assert again.values == r.values


def test_log_bins_and_scale():
    r = Reservoir()
    for v in (1, 2, 10, 100, 1000, 10_000):
        r.add(v)
    d = distribution("h", r, bins=4, scale=0.5, unit="µs")
    assert d.log and (d.min, d.max) == (0.5, 5000)
    assert [b.count for b in d.bins] == [2, 1, 1, 2]
    assert d.bins[1].low == 0.5 * 10 ** 1                    # décades
    assert distribution("vide", Reservoir()).summary() == "vide : aucune valeur"
    one = Reservoir()
    one.add(4)
    assert [b.count for b in distribution("u", one).bins] == [1]
