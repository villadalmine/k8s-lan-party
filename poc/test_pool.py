import unittest
from pool import Pool, client_ip

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


if __name__ == "__main__":
    unittest.main()
