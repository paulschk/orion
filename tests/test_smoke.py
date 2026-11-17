"""Fast, dependency-light checks on the parts that are easy to get wrong.

Run with:  python3 -m unittest discover -s tests -v
Deliberately avoids the database and the network, except where noted.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "test")
# Keep the generated routing table out of the container-only /caddy mount.
os.environ.setdefault("ORION_CADDY_CONFIG", "/tmp/orion-test-data/caddy.json")

from orion import db, identity, provision, sites, util  # noqa: E402
from orion.hostinfo import Allocation, HostInfo, headroom, plan_capacity  # noqa: E402


def setUpModule():
    """The capacity and routing helpers read the database; create it first."""
    db.init()


class TestUtils(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(util.slugify("Mon Super Site !"), "mon-super-site")
        self.assertEqual(util.slugify("éàü"), "server")
        self.assertEqual(util.slugify("a" * 60), "a" * 32)
        self.assertEqual(util.slugify("--x--"), "x")

    def test_money(self):
        # French convention: comma decimal separator, symbol after, nbsp before it.
        self.assertEqual(util.money(300, "EUR"), "3,00\u00a0\u20ac")
        self.assertEqual(util.money(1200, "EUR"), "12,00\u00a0\u20ac")
        self.assertEqual(util.money(1050, "EUR"), "10,50\u00a0\u20ac")
        self.assertEqual(util.money(500, "USD"), "$5.00")

    def test_human_mb(self):
        self.assertEqual(util.human_mb(512), "512 MB")
        self.assertEqual(util.human_mb(1024), "1 GB")
        self.assertEqual(util.human_mb(2048), "2 GB")

    def test_days_from_now_is_in_the_future(self):
        self.assertGreater(util.days_from_now(30), util.now())


class TestBind(unittest.TestCase):
    def test_positional_and_named_params_are_both_accepted(self):
        # tuple/list -> tuple, mapping -> dict, anything else -> 1-tuple
        self.assertEqual(db.bind(("a", "b")), ("a", "b"))
        self.assertEqual(db.bind({"a": 1}), {"a": 1})
        self.assertEqual(db.bind(None), ())
        self.assertEqual(db.bind("solo"), ("solo",))


class TestTemplates(unittest.TestCase):
    def test_template_metadata_is_coherent(self):
        for tpl in provision.template_list():
            self.assertTrue(tpl.slug)
            self.assertTrue(tpl.label)
            self.assertIn(tpl.web, (True, False))

    def test_known_templates_resolve(self):
        for slug in ("bare", "lemp", "wordpress", "node", "docker"):
            self.assertEqual(provision.get_template(slug).slug, slug)

    def test_unknown_template_falls_back_to_bare(self):
        self.assertEqual(provision.get_template("nope").slug, "bare")

    def test_web_templates_serve_port_80(self):
        for tpl in provision.template_list():
            if tpl.web:
                self.assertEqual(tpl.port, 80)

    def test_wordpress_requires_a_real_amount_of_memory(self):
        self.assertGreaterEqual(provision.get_template("wordpress").min_memory_mb, 1024)

    def test_script_substitution_leaves_no_tokens(self):
        plan = {"name": "Standard", "vcpu": 1, "memory_mb": 1024, "disk_gb": 10}
        script = provision.build_script(
            template="lemp", hostname="demo", ssh_user="orion", password="pw",
            authorized_keys="ssh-ed25519 AAAA test@host", plan=plan,
            brand="node29", base_url="https://hosting.node29.fr",
            domain_suffix=".hosting.node29.fr",
        )
        self.assertNotIn("@@", script)
        self.assertIn("ssh-ed25519 AAAA test@host", script)
        # With a key present, password auth must be off.
        self.assertIn("PasswordAuthentication no", script)

    def test_script_without_key_keeps_password_auth(self):
        plan = {"name": "Starter", "vcpu": 1, "memory_mb": 512, "disk_gb": 5}
        script = provision.build_script(
            template="bare", hostname="demo", ssh_user="orion", password="pw",
            authorized_keys="", plan=plan, brand="node29",
            base_url="https://hosting.node29.fr", domain_suffix="",
        )
        self.assertIn("PasswordAuthentication yes", script)


class TestHostMaths(unittest.TestCase):
    def _info(self, **kw):
        base = dict(
            reachable=True, error="", host_name="host", incus_version="7.6", kernel="6.1",
            os_name="Debian", firewall="nftables", cpu_total=4, cpu_model="x", ram_total_mb=16384,
            swap_total_mb=0, disk_total_gb=80, disk_used_gb=1, pool_name="orion",
            pool_total_gb=80, pool_used_gb=1, reserve_vcpu=1, reserve_ram_mb=1536,
            reserve_disk_gb=10, sellable_vcpu=3, sellable_ram_mb=14848,
            sellable_disk_gb=70, detected_at="now",
        )
        base.update(kw)
        return HostInfo(**base)

    def test_capacity_is_limited_by_the_scarcest_resource(self):
        info = self._info()
        # 3 vCPU / 14 GB RAM / 70 GB disk sellable.
        # Small plan: cpu 3, ram 14, disk 7  -> CPU binds.
        self.assertEqual(plan_capacity(info, {"vcpu": 1, "memory_mb": 1024, "disk_gb": 10}), 3)
        # Fat disk plan: cpu 3, ram 7, disk 2 -> disk binds.
        self.assertEqual(plan_capacity(info, {"vcpu": 1, "memory_mb": 2048, "disk_gb": 30}), 2)
        # Fat memory plan: cpu 3, ram 1, disk 7 -> memory binds.
        self.assertEqual(plan_capacity(info, {"vcpu": 1, "memory_mb": 8192, "disk_gb": 10}), 1)
        # Plan bigger than the host in any dimension -> nothing fits.
        self.assertEqual(plan_capacity(info, {"vcpu": 4, "memory_mb": 8192, "disk_gb": 30}), 0)

    def test_capacity_is_zero_when_the_host_is_too_small(self):
        info = self._info(sellable_vcpu=0)
        self.assertEqual(plan_capacity(info, {"vcpu": 1, "memory_mb": 512, "disk_gb": 5}), 0)

    def test_headroom_never_goes_negative(self):
        info = self._info()
        result = headroom(info)
        self.assertIsInstance(result, Allocation)
        self.assertGreaterEqual(result.vcpu, 0)
        self.assertGreaterEqual(result.ram_mb, 0)
        self.assertGreaterEqual(result.disk_gb, 0)


class TestSites(unittest.TestCase):
    def test_default_domain_is_a_subdomain_of_the_panel(self):
        domain = sites.default_domain("vitrine")
        self.assertTrue(domain.startswith("vitrine."))
        self.assertEqual(domain.count("."), sites.default_domain("x").count("."))

    def test_caddy_config_is_json_serialisable_and_has_the_expected_shape(self):
        import json

        config = sites.build_caddy_config()
        json.dumps(config)  # must not raise
        self.assertIn("apps", config)
        server = config["apps"]["http"]["servers"]["orion"]
        self.assertTrue(server["routes"])
        # Last route is always the catch-all 404.
        self.assertNotIn("match", server["routes"][-1])
        self.assertEqual(server["routes"][-1]["handle"][0]["status_code"], 404)

    def test_tenant_listen_port_matches_configuration(self):
        from orion.config import settings

        config = sites.build_caddy_config()
        self.assertEqual(config["apps"]["http"]["servers"]["orion"]["listen"], [settings.tenant_listen])


class TestIdentity(unittest.TestCase):
    def test_derives_from_the_slug(self):
        self.assertEqual(identity.derive_ssh_user("vitrine", set()), "vitrine")

    def test_never_returns_a_reserved_account(self):
        for name in ("root", "admin", "www-data", "orion", "sshd"):
            got = identity.derive_ssh_user(name, set())
            self.assertNotIn(got, identity.RESERVED, f"{name} -> {got}")

    def test_avoids_collisions(self):
        first = identity.derive_ssh_user("vitrine", set())
        second = identity.derive_ssh_user("vitrine", {first})
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("vitrine"))

    def test_sanitises_hostile_input(self):
        self.assertEqual(identity.derive_ssh_user("ANGRY'; rm -rf / #", set()), "angryrm-rf")
        self.assertEqual(identity.derive_ssh_user("", set()), "serveur")
        self.assertEqual(identity.derive_ssh_user("!!!", set()), "serveur")

    def test_prefixes_leading_digits_and_caps_length(self):
        self.assertTrue(identity.derive_ssh_user("29-site", set()).startswith("u29-site"))
        self.assertLessEqual(len(identity.derive_ssh_user("x" * 80, set())), identity.MAX_LEN)

    def test_machine_id_shape(self):
        mid = identity.new_machine_id()
        self.assertTrue(mid.startswith("m-"))
        self.assertEqual(len(mid), 8)
        self.assertNotEqual(mid, identity.new_machine_id())

    def test_ssh_config_block_is_usable(self):
        block = identity.ssh_config_block(alias="vitrine", host="hosting.node29.fr",
                                          port=20001, user="vitrine")
        for expected in ("Host vitrine", "HostName hosting.node29.fr",
                         "Port 20001", "User vitrine", "IdentitiesOnly yes"):
            self.assertIn(expected, block)

    def test_ssh_command_matches_the_documented_form(self):
        self.assertEqual(
            identity.ssh_command(host="hosting.node29.fr", port=20001, user="vitrine"),
            "ssh -p 20001 vitrine@hosting.node29.fr")


if __name__ == "__main__":
    unittest.main()
