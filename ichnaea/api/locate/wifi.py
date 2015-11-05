"""Search implementation using a wifi database."""

from collections import defaultdict, namedtuple
from operator import attrgetter

import numpy
from sqlalchemy.orm import load_only
from sqlalchemy.sql import or_

from ichnaea.api.locate.constants import (
    DataSource,
    MAX_WIFI_CLUSTER_METERS,
    MIN_WIFIS_IN_CLUSTER,
    MAX_WIFIS_IN_CLUSTER,
    WIFI_MAX_ACCURACY,
    WIFI_MIN_ACCURACY,
)
from ichnaea.api.locate.result import Position
from ichnaea.api.locate.source import PositionSource
from ichnaea.constants import (
    PERMANENT_BLOCKLIST_THRESHOLD,
    TEMPORARY_BLOCKLIST_DURATION,
)
from ichnaea.geocalc import (
    aggregate_position,
    distance,
)
from ichnaea.models import WifiShard
from ichnaea import util

Network = namedtuple('Network', 'mac lat lon radius signal')


def cluster_wifis(wifis):
    distance_matrix = [
        [distance(a.lat, a.lon, b.lat, b.lon)
         for a in wifis] for b in wifis]
    clusters = [[i] for i in range(len(wifis))]

    def cluster_distance(a, b):
        return min([distance_matrix[i][j] for i in a for j in b])

    merged_one = True
    while merged_one:
        merged_one = False
        for i in range(len(clusters)):
            if merged_one:
                break
            for j in range(len(clusters)):
                if merged_one:
                    break
                if i == j:
                    continue
                a = clusters[i]
                b = clusters[j]
                if cluster_distance(a, b) <= MAX_WIFI_CLUSTER_METERS:
                    clusters.pop(j)
                    a.extend(b)
                    merged_one = True

    return [[wifis[i] for i in c] for c in clusters]


def get_clusters(wifis, lookups):
    """
    Given a list of wifi models and wifi lookups, return
    a list of clusters of nearby wifi networks.
    """

    # Create a dict of WiFi macs mapped to their signal strength.
    # Estimate signal strength at -100 dBm if none is provided,
    # which is worse than the 99th percentile of wifi dBms we
    # see in practice (-98).
    wifi_signals = {}
    for lookup in lookups:
        wifi_signals[lookup.mac] = lookup.signal or -100

    wifi_networks = [
        Network(w.mac, w.lat, w.lon, w.radius, wifi_signals[w.mac])
        for w in wifis]

    # Sort networks by signal strengths in query.
    wifi_networks.sort(key=attrgetter('signal'), reverse=True)

    clusters = cluster_wifis(wifi_networks)

    # Only consider clusters that have at least 2 found networks
    # inside them. Otherwise someone could use a combination of
    # one real network and one fake and therefor not found network to
    # get the position of the real network.
    return [c for c in clusters if len(c) >= MIN_WIFIS_IN_CLUSTER]


def pick_best_cluster(clusters):
    """
    Out of the list of possible clusters, pick the best one.

    Currently we pick the cluster with the most found networks inside
    it. If we find more than one cluster, we have some stale data in
    our database, as a device shouldn't be able to pick up signals
    from networks more than
    :data:`ichnaea.api.locate.constants.MAX_WIFI_CLUSTER_METERS` apart.
    We assume that the majority of our data is correct and discard the
    minority match.

    The list of clusters is pre-sorted by signal strength, so given
    two clusters with two networks each, the cluster with the better
    signal strength readings wins.
    """
    def sort_cluster(cluster):
        return len(cluster)

    return sorted(clusters, key=sort_cluster, reverse=True)[0]


def aggregate_cluster_position(cluster, result_type):
    """
    Given a single cluster, return the aggregate position of the user
    inside the cluster.

    We take at most
    :data:`ichnaea.api.locate.constants.MAX_WIFIS_IN_CLUSTER`
    of of the networks in the cluster when estimating the aggregate
    position.

    The reason is that we're doing a (non-weighted) centroid calculation,
    which is itself unbalanced by distant elements. Even if we did a
    weighted centroid here, using radio intensity as a proxy for
    distance has an error that increases significantly with distance,
    so we'd have to underweight pretty heavily.
    """
    sample = cluster[:min(len(cluster), MAX_WIFIS_IN_CLUSTER)]
    circles = numpy.array(
        [(wifi.lat, wifi.lon, wifi.radius) for wifi in sample],
        dtype=numpy.double)
    lat, lon, accuracy = aggregate_position(circles, WIFI_MIN_ACCURACY)
    accuracy = min(accuracy, WIFI_MAX_ACCURACY)
    return result_type(lat=lat, lon=lon, accuracy=accuracy)


def query_wifis(query, raven_client):
    macs = [lookup.mac for lookup in query.wifi]
    if not macs:  # pragma: no cover
        return []

    result = []
    today = util.utcnow().date()
    temp_blocked = today - TEMPORARY_BLOCKLIST_DURATION

    try:
        load_fields = ('lat', 'lon', 'radius')
        shards = defaultdict(list)
        for mac in macs:
            shards[WifiShard.shard_model(mac)].append(mac)

        for shard, shard_macs in shards.items():
            rows = (
                query.session.query(shard)
                             .filter(shard.mac.in_(shard_macs))
                             .filter(shard.lat.isnot(None))
                             .filter(shard.lon.isnot(None))
                             .filter(or_(
                                 shard.block_count.is_(None),
                                 shard.block_count <
                                     PERMANENT_BLOCKLIST_THRESHOLD))
                             .filter(or_(
                                 shard.block_last.is_(None),
                                 shard.block_last < temp_blocked))
                             .options(load_only(*load_fields))
            ).all()
            result.extend(list(rows))
    except Exception:
        raven_client.captureException()
    return result


class WifiPositionMixin(object):
    """
    A WifiPositionMixin implements a position search using
    the WiFi models and a series of clustering algorithms.
    """

    raven_client = None
    result_type = Position

    def should_search_wifi(self, query, results):
        return bool(query.wifi)

    def search_wifi(self, query):
        result = self.result_type()
        if not query.wifi:
            return result

        wifis = query_wifis(query, self.raven_client)
        clusters = get_clusters(wifis, query.wifi)
        if clusters:
            cluster = pick_best_cluster(clusters)
            result = aggregate_cluster_position(cluster, self.result_type)

        return result


class WifiPositionSource(WifiPositionMixin, PositionSource):
    """
    Implements a search using our wifi data.

    This source is only used in tests.
    """

    fallback_field = None  #:
    source = DataSource.internal

    def should_search(self, query, results):
        return self.should_search_wifi(query, results)

    def search(self, query):
        return self.search_wifi(query)
