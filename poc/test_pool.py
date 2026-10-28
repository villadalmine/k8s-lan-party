import unittest
from pool import DAY, Pool, client_ip, identity

CFG = {"slots": 1, "lease_seconds": 3600, "cooldown_seconds": 3600, "rate_window_seconds": 600,
       "rate_max_requests": 3, "trusted_proxies": ["127.0.0.1"]}


class PoolTest(unittest.TestCase):
    def test_join_then_full_goes_to_waitlist_fifo(self):
        p = Pool(CFG)
        self.assertEqual(p.request("1.1.1.1", 0)[0], "granted")
        self.assertEqual(p.request("2.2.2.2", 1), ("waitlist", 1))
        self.assertEqual(p.request("3.3.3.3", 2), ("waitlist", 2))
        self.assertEqual(p.request("2.2.2.2", 3), ("waitlist", 1))   # idempotent position

    def test_same_ip_refused_while_active(self):
        p = Pool(CFG)
        p.request("1.1.1.1", 0)
        self.assertEqual(p.request("1.1.1.1", 5), ("refused", "ip-has-active-lease"))

    def test_expiry_frees_slot_for_head_of_waitlist_and_starts_cooldown(self):
        p = Pool(CFG)
        p.request("1.1.1.1", 0)
        p.request("2.2.2.2", 1)
        self.assertEqual(p.expired(3599), [])
        self.assertEqual(p.expired(3600), ["lease-1"])
        lease, promoted = p.reap("lease-1", 3600)
        self.assertEqual(promoted["ip"], "2.2.2.2")
        self.assertEqual(promoted["end"], 7200)
        self.assertEqual(p.request("1.1.1.1", 3601), ("refused", "cooldown"))
        self.assertEqual(p.request("1.1.1.1", 3600 + 3600)[0], "waitlist")  # cooldown over, pool full

    def test_rate_limit(self):
        p = Pool(CFG)
        for t in range(3):
            p.request("9.9.9.9", t)
        self.assertEqual(p.request("9.9.9.9", 4), ("refused", "rate-limited"))
        self.assertNotEqual(p.request("9.9.9.9", 700)[1], "rate-limited")  # window passed

    def test_xff_only_from_trusted_proxy(self):
        self.assertEqual(client_ip("127.0.0.1", "8.8.8.8", ["127.0.0.1"]), "8.8.8.8")
        self.assertEqual(client_ip("5.5.5.5", "8.8.8.8", ["127.0.0.1"]), "5.5.5.5")   # spoof ignored
        self.assertEqual(client_ip("127.0.0.1", "6.6.6.6, 8.8.8.8", ["127.0.0.1"]), "8.8.8.8")

    def test_kick_equals_expiry(self):
        p = Pool(CFG)
        p.request("1.1.1.1", 0)
        lease, promoted = p.reap("lease-1", 10)
        self.assertIsNone(promoted)
        self.assertEqual(p.status(10)["slots_free"], 1)


CAP = dict(CFG, slots=2, daily_leases_per_identity=2, rate_max_requests=100)


class DailyCapTest(unittest.TestCase):
    """At most 2 leases per identity in any 24 h, a ROLLING window (docs/ADMISSION.md: no midnight reset to game)."""

    def cycle(self, p, ip, t):
        self.assertEqual(p.request(ip, t)[0], "granted")
        lease = next(l for l in p.leases if l["ip"] == ip)
        p.reap(lease["id"], lease["end"])
        return lease["end"] + CFG["cooldown_seconds"]   # when the cooldown is over

    def test_third_lease_in_24h_is_refused_until_the_oldest_leaves_the_window(self):
        p = Pool(CAP)
        t = self.cycle(p, "1.1.1.1", 0)        # lease 1 at 0
        t = self.cycle(p, "1.1.1.1", t)        # lease 2 at 7200, cooldown over at 14400
        self.assertEqual(p.request("1.1.1.1", t), ("refused", "daily-cap"))
        self.assertEqual(p.eligible_at("1.1.1.1", t), (DAY, "daily-cap"))
        self.assertEqual(p.request("1.1.1.1", DAY - 1), ("refused", "daily-cap"))
        self.assertEqual(p.request("1.1.1.1", DAY)[0], "granted")    # lease 1 left the window

    def test_rolling_not_calendar_day(self):
        # two leases just before midnight UTC: a calendar-day cap would allow two more just after it; the rolling one does not
        p = Pool(dict(CAP, cooldown_seconds=0))
        midnight = 20 * DAY
        p.request("2.2.2.2", midnight - 7200)
        p.reap("lease-1", midnight - 3600)
        p.request("2.2.2.2", midnight - 3600)
        p.reap("lease-2", midnight)
        self.assertEqual(p.request("2.2.2.2", midnight + 60), ("refused", "daily-cap"))
        self.assertEqual(p.eligible_at("2.2.2.2", midnight + 60), (midnight - 7200 + DAY, "daily-cap"))

    def test_cap_is_per_identity(self):
        p = Pool(CAP)
        t = self.cycle(p, "1.1.1.1", 0)
        self.cycle(p, "1.1.1.1", t)
        self.assertEqual(p.request("3.3.3.3", t)[0], "granted")

    def test_cooldown_holds_before_the_cap(self):
        p = Pool(CAP)
        self.cycle(p, "1.1.1.1", 0)
        self.assertEqual(p.eligible_at("1.1.1.1", 3600), (7200, "cooldown"))
        self.assertEqual(p.eligible_at("1.1.1.1", 7200), (7200, ""))

    def test_active_lease_holds_until_end_plus_cooldown(self):
        p = Pool(CAP)
        p.request("1.1.1.1", 0)
        self.assertEqual(p.eligible_at("1.1.1.1", 10), (7200, "active-lease"))

    def test_promotion_skips_an_identity_over_its_cap_and_keeps_it_in_line(self):
        p = Pool(dict(CAP, slots=1, cooldown_seconds=0))
        p.request("1.1.1.1", 0); p.reap("lease-1", 3600)
        p.request("1.1.1.1", 3600)                 # lease 2 of 1.1.1.1
        p.waitlist = ["1.1.1.1", "4.4.4.4"]        # 1.1.1.1 got back in line somehow (old state)
        _, promoted = p.reap("lease-2", 7200)
        self.assertEqual(promoted["ip"], "4.4.4.4")
        self.assertEqual(p.waitlist, ["1.1.1.1"])

    def test_grant_refuses_without_a_free_slot(self):
        p = Pool(dict(CAP, slots=1))
        p.grant("1.1.1.1", 0)
        with self.assertRaises(ValueError):
            p.grant("2.2.2.2", 0)

    def test_old_state_without_grants_loads(self):
        p = Pool(CAP, {"leases": [], "waitlist": [], "cooldown": {}, "requests": {}, "next_id": 7})
        self.assertEqual(p.request("1.1.1.1", 0)[1]["id"], "lease-7")


class IdentityTest(unittest.TestCase):
    def test_ipv4_whole_address_by_default(self):
        self.assertEqual(identity("203.0.113.7"), "203.0.113.7")

    def test_ipv4_slash_24_when_configured(self):
        self.assertEqual(identity("203.0.113.7", v4_prefix=24), "203.0.113.0/24")
        self.assertEqual(identity("203.0.113.200", v4_prefix=24), identity("203.0.113.7", v4_prefix=24))

    def test_ipv6_slash_64(self):
        self.assertEqual(identity("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64")
        self.assertEqual(identity("2001:DB8:1:2:ffff::9"), identity("2001:db8:1:2::1"))
        self.assertNotEqual(identity("2001:db8:1:3::1"), identity("2001:db8:1:2::1"))

    def test_ipv4_mapped_ipv6_counts_as_ipv4(self):
        self.assertEqual(identity("::ffff:203.0.113.7"), "203.0.113.7")

    def test_no_ip_falls_back_to_the_name(self):
        self.assertEqual(identity(None, "alice"), "name:alice")
        self.assertEqual(identity("", "alice"), "name:alice")

    def test_not_an_ip_is_refused(self):
        for bad in ("alice", "1.2.3", "1.2.3.4; rm -rf /", "999.1.1.1"):
            with self.assertRaises(ValueError):
                identity(bad)
        with self.assertRaises(ValueError):
            identity(None, None)

    def test_xff_then_identity(self):
        trusted = ["127.0.0.1"]
        self.assertEqual(identity(client_ip("127.0.0.1", "2001:db8:9:9::5", trusted)), "2001:db8:9:9::/64")
        self.assertEqual(identity(client_ip("198.51.100.1", "2001:db8:9:9::5", trusted)), "198.51.100.1")  # spoof ignored
        self.assertEqual(identity(client_ip("127.0.0.1", "10.0.0.9, 127.0.0.1", trusted)), "10.0.0.9")    # proxy hop skipped


if __name__ == "__main__":
    unittest.main()
