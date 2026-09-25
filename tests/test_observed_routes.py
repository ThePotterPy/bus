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

    def test_chunks_from_one_passage_share_one_match_request(self):
        for i in range(50):
            self.store.observe('30', [{'unit':'long', 'lat':-25.002,
                'lon':-57+i*.0003, 'route':'Azul (I)'}], NOW+i*10)
        self.store.flush_idle(NOW+700)
        with self.store.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM observed_jobs').fetchone()[0]
        self.assertGreater(before, 1)
        calls = []
        def match_once(points):
            calls.append(points)
            return self.match(points)
        self.assertTrue(self.store.process_one(match_once, NOW+800))
        self.assertEqual(len(calls), 1)
        self.assertGreater(len(calls[0]), 24)
        with self.store.connect() as db:
            statuses = {row['status']: row['total'] for row in db.execute(
                'SELECT status,COUNT(*) AS total FROM observed_jobs GROUP BY status')}
        self.assertEqual(statuses.get('done'), 1)
        self.assertEqual(statuses.get('merged'), before-1)

    def test_archived_history_survives_cleanup(self):
        self.travel(); self.process()
        later = NOW+WINDOW+1000
        self.store.maintain(later)
        self.assertFalse(self.snap(now=later)['alternatives'])
        archived = self.snap(now=later, history=True)['alternatives']
        self.assertTrue(archived)
        self.assertTrue(all(e['archived'] and e['buses']==0 and e['historical_buses']==1 for e in archived))

    def test_old_passages_are_compacted_without_inflating_bus_history(self):
        self.travel(); self.process()
        later = NOW+91*86400
        self.store.maintain(later)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_passages').fetchone()[0], 0)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM observed_passage_archive').fetchone()[0], 0)
        self.travel(start=later)
        self.process(later+300)
        archived = self.snap(now=later+301, history=True)['alternatives']
        self.assertTrue(archived)
        self.assertTrue(all(edge['historical_buses'] == 1 and edge['buses'] == 1
                            for edge in archived))

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

    def test_tomtom_candidate_requires_human_approval_and_can_be_retracted(self):
        self.travel()
        class Matcher:
            last_quality = {'minimum_confidence': .96, 'matched_ratio': 1}
            last_details = {'projected_points': [{'index': 0, 'point': [-25.002, -57], 'offset_m': 0}],
                            'segments': [{'confidence': .96, 'roadUse': 'Local'}]}
            def __call__(self, points):
                return [[p[:2] for p in points]]
        self.store.match_provider = 'tomtom'
        self.store.tomtom_matcher = Matcher()
        self.store.shadow_mode = False
        self.assertTrue(self.store.process_one(now=NOW+300))
        review = self.store.review('awaiting_review', now=NOW+301)
        self.assertEqual(len(review['items']), 1)
        item = review['items'][0]
        self.assertEqual(item['matching_details']['segments'][0]['roadUse'], 'Local')
        self.assertFalse(self.snap()['alternatives'])
        self.assertEqual(self.store.decide_review(item['id'], 'approve', now=NOW+302)['status'], 'approved')
        self.assertTrue(self.snap()['alternatives'])
        self.assertFalse(self.store.decide_review(item['id'], 'approve', now=NOW+303)['changed'])
        self.assertEqual(self.store.decide_review(item['id'], 'discard', 'Cruza una casa', NOW+304)['status'], 'discarded')
        self.assertFalse(self.snap()['alternatives'])
        self.assertEqual(self.store.review('discarded', now=NOW+305)['items'][0]['review_note'], 'Cruza una casa')

    def test_tomtom_rate_limit_is_not_treated_as_bad_geometry(self):
        from urllib.error import HTTPError
        self.travel()
        class Matcher:
            last_quality = {}
            last_details = {}
            def __call__(self, _points):
                raise HTTPError('https://api.tomtom.com/snapToRoads/1', 429, 'Too Many Requests',
                                {'Retry-After': '7'}, None)
        self.store.match_provider = 'tomtom'
        self.store.tomtom_matcher = Matcher()
        self.assertFalse(self.store.process_one(now=NOW+300))
        with self.store.connect() as db:
            row = db.execute('SELECT status,attempts,next_try FROM observed_jobs').fetchone()
            audit = db.execute('SELECT result FROM observed_match_audit').fetchone()
        self.assertEqual((row['status'], row['attempts'], audit['result']), ('pending', 0, 'rate_limited'))
        self.assertEqual(row['next_try'], NOW+307)
        self.assertEqual(self.store.run_match_batch(limit=1, now=NOW+301)['attempted'], 0)

    def test_missed_weekly_run_is_resumed_once_with_persistent_cap(self):
        self.travel()
        self.store.match_provider = 'tomtom'
        self.store.tomtom_matcher = object()
        self.store.weekly_match_limit = 2
        with self.store.connect() as db:
            previous = db.execute('SELECT MAX(scheduled_at) FROM observed_weekly_runs').fetchone()[0]
        with patch.object(self.store, 'process_one', return_value=True) as startup_process:
            self.assertEqual(self.store.run_due_weekly(now=previous+1)['attempted'], 0)
        startup_process.assert_not_called()
        missed = previous + 7*86400 + 1
        with patch.object(self.store, 'process_one', return_value=True) as process:
            first = self.store.run_due_weekly(now=missed)
            second = self.store.run_due_weekly(now=missed+3600)
        self.assertEqual(first['attempted'], 2)
        self.assertEqual(second['attempted'], 0)
        self.assertEqual(process.call_count, 2)
        reopened = ObservedRoutes(self.db, match_provider='tomtom', weekly_match_limit=2)
        reopened.tomtom_matcher = object()
        reopened.init()
        with patch.object(reopened, 'process_one', return_value=True) as process_after_restart:
            self.assertEqual(reopened.run_due_weekly(now=missed+7200)['attempted'], 0)
        process_after_restart.assert_not_called()

    def test_tomtom_refinement_is_an_explicit_alternative(self):
        self.travel()
        class Matcher:
            last_quality = {}
            last_details = {}
            calls = 0
            def __call__(self, points):
                self.calls += 1
                self.last_quality = {'matched_ratio': 1}
                self.last_details = ({'projected_points': [
                    {'index': 3, 'point': points[3][:2], 'offset_m': 35}]}
                    if self.calls == 1 else {'projected_points': []})
                shift = .001 if self.calls == 2 else 0
                return [[[point[0]-shift, point[1]] for point in points]]
        self.store.match_provider = 'tomtom'
        matcher = Matcher()
        self.store.tomtom_matcher = matcher
        self.assertTrue(self.store.process_one(now=NOW+300))
        item = self.store.review('awaiting_review', now=NOW+301)['items'][0]
        original = item['geometry']
        self.assertEqual(self.store.refine_review(item['id'], now=NOW+302)['removed_gps_points'], 1)
        self.assertFalse(self.store.refine_review(item['id'], now=NOW+303)['changed'])
        item = self.store.review('awaiting_review', now=NOW+304)['items'][0]
        self.assertEqual(item['geometry'], original)
        self.assertNotEqual(item['refinement']['geometry'], original)
        self.assertEqual(item['refinement']['metrics']['removed_gps_points'], [3])
        self.assertEqual(matcher.calls, 2)
        self.store.decide_review(item['id'], 'approve', now=NOW+305, variant='refined')
        approved = self.store.review('approved', now=NOW+306)['items'][0]
        self.assertEqual(approved['approved_variant'], 'refined')
        with self.assertRaisesRegex(ValueError, 'descartá primero'):
            self.store.decide_review(item['id'], 'approve', now=NOW+307, variant='original')

    def test_discarding_one_review_preserves_other_approved_passages(self):
        self.travel(unit='1')
        self.travel(unit='2', start=NOW+500)
        class Matcher:
            last_quality = {}
            last_details = {}
            def __call__(self, points):
                return [[p[:2] for p in points]]
        self.store.match_provider = 'tomtom'
        self.store.tomtom_matcher = Matcher()
        self.assertTrue(self.store.process_one(now=NOW+900))
        self.assertTrue(self.store.process_one(now=NOW+901))
        ids = [item['id'] for item in self.store.review('awaiting_review', now=NOW+902)['items']]
        self.assertEqual(len(ids), 2)
        for job_id in ids:
            self.store.decide_review(job_id, 'approve', now=NOW+903)
        self.store.decide_review(ids[0], 'discard', now=NOW+904)
        with self.store.connect() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM observed_passages').fetchone()[0], 0)
        self.store.decide_review(ids[1], 'discard', now=NOW+905)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_passages').fetchone()[0], 0)

    def test_overlapping_approvals_restore_original_passage_time(self):
        self.travel()
        class Matcher:
            last_quality = {}
            last_details = {}
            def __call__(self, points):
                return [[p[:2] for p in points]]
        self.store.match_provider = 'tomtom'
        self.store.tomtom_matcher = Matcher()
        self.assertTrue(self.store.process_one(now=NOW+300))
        with self.store.connect() as db:
            first = db.execute("SELECT * FROM observed_jobs WHERE status='done'").fetchone()
            db.execute('''INSERT INTO observed_jobs
                (id,line,unit,route,passage,kind,points,seen)
                VALUES(?,?,?,?,?,?,?,?)''',
                ('b' * 64, first['line'], first['unit'], first['route'], first['passage'],
                 first['kind'], first['points'], first['seen']+100))
        self.assertTrue(self.store.process_one(now=NOW+500))
        self.store.decide_review(first['id'], 'approve', now=NOW+501)
        self.store.decide_review('b' * 64, 'approve', now=NOW+502)
        self.store.decide_review('b' * 64, 'discard', now=NOW+503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT MAX(seen) FROM observed_passages').fetchone()[0], first['seen'])
        self.store.decide_review(first['id'], 'discard', now=NOW+504)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observed_passages').fetchone()[0], 0)

    def test_tomtom_requeues_recent_legacy_provider_failures_once(self):
        self.travel()
        with self.store.connect() as db:
            db.execute("UPDATE observed_jobs SET status='failed',attempts=8,error='legacy OSRM'")
        replacement = ObservedRoutes(self.db, match_provider='tomtom', tomtom_api_key='secret')
        with patch('observed_routes.time.time', return_value=NOW+300):
            replacement.init()
        with replacement.connect() as db:
            job = db.execute('SELECT status,attempts,error FROM observed_jobs').fetchone()
        self.assertEqual((job['status'], job['attempts'], job['error']), ('pending', 0, ''))

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
