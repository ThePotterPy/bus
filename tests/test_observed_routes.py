import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from observed_routes import ObservedRoutes, WINDOW, sample_time, street_edges


NOW = 1800000000


def official(lat=-25.0):
    return {'services': [{'routes': [{'name': 'Azul (I)', 'sections': [{'traces': [
        {'order': 1, 'latitud': lat, 'longitud': -57.01},
        {'order': 2, 'latitud': lat, 'longitud': -56.99},
    ]}]}]}]}


class ObservedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservedRoutes(Path(self.tmp.name)/'test.sqlite3')
        self.store.init()
        self.store.save_official('30', official())

    def tearDown(self):
        self.tmp.cleanup()

    def travel(self, unit='1', line='30', route='Azul (I)', start=NOW, lat=-25.002, count=8):
        for i in range(count):
            self.store.observe(line, [{'unit': unit, 'lat': lat, 'lon': -57+i*.0003, 'route': route}], start+i*10)
        self.store.flush_idle(start+count*10+100)

    @staticmethod
    def match(points):
        return [[p[:2] for p in points]]

    def process(self, now=NOW+300):
        while self.store.process_one(self.match, now): pass

    def snap(self, line='30', now=NOW+301, history=False):
        self.store.cache.clear()
        return self.store.snapshot(line, history, now)

    def test_same_bus_and_many_reads_never_inflate_distinct_count(self):
        self.travel()
        self.travel(start=NOW+500)
        self.process(NOW+1000)
        for _ in range(5):
            edges = self.snap(now=NOW+1001)['alternatives']
            self.assertTrue(edges)
            self.assertEqual({e['buses'] for e in edges}, {1})
            self.assertEqual({e['passes'] for e in edges}, {2})

    def test_four_distinct_buses_and_provider_line_isolation(self):
        for unit in ('1', '2', '3', '4'): self.travel(unit)
        self.store.save_official('12', official())
        self.travel('5', line='12')
        self.process()
        self.assertEqual({e['buses'] for e in self.snap()['alternatives']}, {4})
        self.assertEqual({e['buses'] for e in self.snap('12')['alternatives']}, {1})

    def test_ramales_and_numbered_branches_never_mix(self):
        for i, route in enumerate(('Azul 1 (I)', 'Azul 2 (I)')):
            self.travel(unit=str(i+1), route=route, start=NOW+i*1000)
        self.process(NOW+2500)
        edges = self.snap(now=NOW+2501)['alternatives']
        self.assertEqual({e['route'] for e in edges}, {'Azul 1 (I)', 'Azul 2 (I)'})
        self.assertEqual({e['buses'] for e in edges}, {1})

    def test_restart_retains_active_state_and_idempotent_observation(self):
        for i in range(4):
            self.store.observe('30', [{'unit':'1', 'lat':-25.002, 'lon':-57+i*.0003, 'route':'Azul (I)'}], NOW+i*10)
        self.store = ObservedRoutes(self.store.path)
        self.store.init()
        self.assertEqual(len(self.snap(now=NOW+35)['tails']), 1)
        self.store.observe('30', [{'unit':'1', 'lat':-25.002, 'lon':-57+.0009, 'route':'Azul (I)'}], NOW+30)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_samples').fetchone()[0], 4)

    def test_official_unchanged_no_alternative_on_route(self):
        self.travel(lat=-25)
        with self.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM observed_official').fetchone()[0]), official())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_jobs').fetchone()[0], 0)

    def test_missing_official_is_observed_but_failed_download_is_unknown(self):
        self.store.save_official('empty', {'services': []})
        self.travel(line='empty')
        self.travel(line='unknown')
        self.process()
        self.assertEqual({e['kind'] for e in self.snap('empty')['alternatives']}, {'observed'})
        self.assertFalse(self.snap('unknown')['alternatives'])

    def test_empty_refresh_does_not_erase_previous_official(self):
        self.store.save_official('30', {'services': []})
        self.assertTrue(self.store.indexes['30'].cells)

    def test_matching_failure_keeps_pending_with_backoff(self):
        self.travel()
        def fail(_): raise ValueError('temporary outage')
        self.assertFalse(self.store.process_one(fail, NOW+300))
        with self.store.connect() as db:
            job = db.execute('SELECT * FROM observed_jobs').fetchone()
            self.assertEqual(job['status'], 'pending')
            self.assertGreater(job['next_try'], NOW+300)
        self.assertTrue(self.snap()['pending'])
        self.assertTrue(self.store.process_one(self.match, NOW+1000))

    def test_archived_history_survives_cleanup(self):
        self.travel(); self.process()
        later = NOW+WINDOW+1000
        self.store.maintain(later)
        self.assertFalse(self.snap(now=later)['alternatives'])
        archived = self.snap(now=later, history=True)['alternatives']
        self.assertTrue(archived)
        self.assertTrue(all(e['archived'] and e['buses']==0 and e['historical_buses']==1 for e in archived))

    def test_stale_offline_and_teleport_do_not_form_trace(self):
        self.assertIsNone(sample_time({'online':0}, NOW))
        self.assertIsNone(sample_time({'modified':'2020-01-01T00:00:00Z'}, NOW))
        for i in range(8):
            self.store.observe('30', [{'unit':'1','lat':-25.002-i*.1,'lon':-57}], NOW+i*10)
        self.store.flush_idle(NOW+200)
        self.assertFalse(self.snap()['pending'])

    def test_return_to_route_preserves_completed_trail(self):
        for i in range(8):
            self.store.observe('30', [{'unit':'1','lat':-25.002,'lon':-57+i*.0003}], NOW+i*10)
        for i in range(2):
            self.store.observe('30', [{'unit':'1','lat':-25,'lon':-56.997+i*.0003}], NOW+90+i*10)
        data = self.snap(now=NOW+110)
        self.assertFalse(data['tails'])
        self.assertTrue(data['pending'])

    def test_canonical_edges_handle_extra_collinear_points_and_direction(self):
        a, b, c = [-25.002,-57], [-25.002,-56.999], [-25.002,-56.998]
        forward = {k for k,_ in street_edges([a,c])}
        self.assertEqual(forward, {k for k,_ in street_edges([a,b,c])})
        self.assertFalse(forward & {k for k,_ in street_edges([c,a])})

    def test_partial_overlap_counts_only_shared_geometry(self):
        self.travel('1', count=8)
        self.travel('2', count=5)
        self.process()
        self.assertEqual({e['buses'] for e in self.snap()['alternatives']}, {1,2})

    def test_low_confidence_match_rejected(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps({'code':'Ok','matchings':[{'confidence':.2}]}).encode()
        self.store.osrm_url = 'https://example.test'
        with patch('observed_routes.urllib.request.urlopen', return_value=Response()):
            with self.assertRaises(ValueError): self.store.match_geometry([[-25,-57,NOW],[-25,-56.999,NOW+10],[-25,-56.998,NOW+20]])


if __name__ == '__main__':
    unittest.main()
