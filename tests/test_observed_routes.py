import json
import unittest
from pathlib import Path
from unittest.mock import patch

from observed_routes import ObservedRoutes, TomTomMatcher, WINDOW, gps_spike, sample_time, street_edges


NOW = 1800000000


def official(lat=-25.0):
    return {'services': [{'routes': [{'name': 'Azul (I)', 'sections': [{'traces': [
        {'order': 1, 'latitud': lat, 'longitud': -57.01},
        {'order': 2, 'latitud': lat, 'longitud': -56.99},
    ]}]}]}]}


class ObservedTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(__file__).with_name('_observed_routes_test.sqlite3')
        self._clean_db()
        self.store = ObservedRoutes(self.db)
        self.store.init()
        self.store.save_official('30', official())

    def tearDown(self):
        self._clean_db()

    def _clean_db(self):
        for suffix in ('', '-wal', '-shm'):
            Path(str(self.db) + suffix).unlink(missing_ok=True)

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

    def test_distinct_named_ramales_never_mix(self):
        for i, route in enumerate(('Azul Norte (I)', 'Azul Sur (I)')):
            self.travel(unit=str(i+1), route=route, start=NOW+i*1000)
        self.process(NOW+2500)
        edges = self.snap(now=NOW+2501)['alternatives']
        self.assertEqual({e['route'] for e in edges}, {'Azul Norte (I)', 'Azul Sur (I)'})
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

    def test_job_is_claimed_before_external_matching(self):
        self.travel()
        def inspect_claim(points):
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM observed_jobs').fetchone()['status'], 'processing')
            return self.match(points)
        self.assertTrue(self.store.process_one(inspect_claim, NOW+300))

    def test_archived_history_survives_cleanup(self):
        self.travel(); self.process()
        later = NOW+WINDOW+1000
        self.store.maintain(later)
        self.assertFalse(self.snap(now=later)['alternatives'])
        archived = self.snap(now=later, history=True)['alternatives']
        self.assertTrue(archived)
        self.assertTrue(all(e['archived'] and e['buses']==0 and e['historical_buses']==1 for e in archived))

    def test_unreliable_offline_flag_accepts_real_movement_only(self):
        self.assertEqual(sample_time({'online':0}, NOW), NOW)
        # A frozen coordinate marked offline establishes a baseline but never
        # accumulates the three moving samples required for a trail.
        for i in range(8):
            self.store.observe('30', [{
                'unit':'frozen', 'lat':-25.002, 'lon':-57,
                'route':'Azul (I)', 'online':0,
            }], NOW+i*10)
        self.store.flush_idle(NOW+200)
        self.assertFalse(self.snap()['pending'])

        # Regression for lines such as JAHA 187: online=0 is wrong, but
        # consistent GPS movement must still become shared evidence.
        for i in range(8):
            self.store.observe('30', [{
                'unit':'moving', 'lat':-25.002, 'lon':-57+i*.0003,
                'route':'Azul (I)', 'online':0,
            }], NOW+i*10)
        self.store.flush_idle(NOW+200)
        self.assertTrue(self.snap()['pending'])

    def test_stale_timestamp_and_teleport_do_not_form_trace(self):
        self.assertIsNone(sample_time({'modified':'2020-01-01T00:00:00Z'}, NOW))
        for i in range(8):
            self.store.observe('30', [{'unit':'1','lat':-25.002-i*.1,'lon':-57}], NOW+i*10)
        self.store.flush_idle(NOW+200)
        self.assertFalse(self.snap()['pending'])

    def test_single_gps_spike_is_removed_when_bus_returns(self):
        a = [-25.002, -57, NOW]
        b = [-25.012, -57, NOW+10]
        c = [-25.002, -56.9998, NOW+20]
        self.assertTrue(gps_spike(a, b, c))
        for point in (a, b, c):
            self.store.observe('30', [{'unit':'spike', 'lat':point[0], 'lon':point[1],
                                       'route':'Azul (I)'}], point[2])
        with self.store.connect() as db:
            samples = db.execute("SELECT lat,lon FROM observed_samples WHERE unit='spike' ORDER BY stamp").fetchall()
        self.assertEqual(len(samples), 2)
        self.assertNotIn((-25.012, -57.0), {(row['lat'], row['lon']) for row in samples})

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

    def test_tomtom_adapter_accepts_only_confident_road_geometry(self):
        points = [[-25, -57, NOW], [-25, -56.9997, NOW+10], [-25, -56.9994, NOW+20]]
        route = [[point[1], point[0]] for point in points]
        payload = {
            'projectedPoints': [
                {'geometry': {'coordinates': [point[1], point[0]]},
                 'properties': {'snapResult': 'Matched', 'routeIndex': 0}}
                for point in points
            ],
            'route': [{'geometry': {'coordinates': route},
                       'properties': {'id': 0, 'confidence': .96}}],
            'distances': {'total': 60, 'road': 60, 'offRoad': 0, 'unit': 'm'},
        }
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return json.dumps(payload).encode()
        with patch('observed_routes.urllib.request.urlopen', return_value=Response()) as request:
            parts = TomTomMatcher('server-secret')(points)
        self.assertEqual(parts, [[point[:2] for point in points]])
        sent_request = request.call_args.args[0]
        self.assertIn('vehicleType=Bus', sent_request.full_url)
        self.assertNotIn('server-secret', sent_request.data.decode())

    def test_tomtom_monthly_budget_is_hard_limited(self):
        self.store.match_provider = 'tomtom'
        self.store.monthly_match_limit = 1
        self.assertTrue(self.store._reserve_match_request(NOW))
        self.assertFalse(self.store._reserve_match_request(NOW+1))
        self.assertEqual(self.store.health(NOW)['monthly_requests'], 1)

    def test_shadow_mode_validates_but_does_not_publish(self):
        self.travel()
        self.store.shadow_mode = True
        self.assertTrue(self.store.process_one(self.match, NOW+300))
        with self.store.connect() as db:
            job = db.execute('SELECT status,points,geometry FROM observed_jobs').fetchone()
            self.assertEqual(job['status'], 'done')
            self.assertTrue(json.loads(job['points']))
            self.assertTrue(json.loads(job['geometry']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_edges').fetchone()[0], 0)
        review = self.store.review('accepted', now=NOW+301)
        self.assertEqual(len(review['items']), 1)
        self.assertTrue(review['items'][0]['points'])
        self.assertEqual(review['health']['accepted_last_30_days'], 1)
        self.store.maintain(NOW+8*86400)
        with self.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT points FROM observed_jobs').fetchone()[0]), [])

    def test_public_snapshot_requires_configured_evidence(self):
        self.store.min_confirmed_buses = 2
        self.store.min_confirmed_passes = 2
        self.travel('only'); self.process()
        self.assertFalse(self.snap()['alternatives'])
        self.travel('second', start=NOW+500); self.process(NOW+1000)
        self.assertTrue(self.snap(now=NOW+1001)['alternatives'])


if __name__ == '__main__':
    unittest.main()
